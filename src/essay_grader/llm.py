"""Anthropic structured-output wrapper (json_schema output_config + pydantic validation), usage/cost, retries.

Request shape for the Claude 5.5 models: NO forced `tool_choice` (HTTP 400), NO temperature/top_p/top_k, NO
`thinking={"type":"disabled"}`. Structured output uses `output_config={"format": {"type": "json_schema", ...}}` and
`output_config.effort` is always set explicitly. Server-side fallback is enabled via the beta
`server-side-fallback-2026-07-01` (`fallbacks="default"`).

`client.beta.messages.parse` exists in the installed SDK, but it parses the first text block before the caller can
look at `stop_reason`; a refusal would raise instead of returning a structured error. So we call `create` and parse the
JSON text ourselves, after checking for a refusal.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
SONNET_MODEL = "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
Effort = Literal["low", "medium", "high"]
T = TypeVar("T")

# USD per million tokens: (input, output, cache read). Anthropic pricing as of Oct 2026; OpenAI Decisions guide for
# gpt-6-luna (no output or cache charges; regional/long-context multipliers not modeled). A cache-read price of None
# means "no cache discount" (billed at the input price). Cache writes are billed at ~1.25x input (CACHE_WRITE_FACTOR).
# Unknown models report cost as null and are not gated by a Budget.
PRICE_PER_MTOK: dict[str, tuple[float, float, float | None]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, None),
    "gpt-6-luna": (0.10, 0.0, None),
}
CACHE_WRITE_FACTOR = 1.25


def price_for(model: str) -> tuple[float, float, float | None] | None:
    """Exact match, else the longest key that is a prefix of `model` (dated snapshot ids like claude-opus-5-5-2026...)."""
    if model in PRICE_PER_MTOK:
        return PRICE_PER_MTOK[model]
    keys = [k for k in PRICE_PER_MTOK if model.startswith(k)]
    return PRICE_PER_MTOK[max(keys, key=len)] if keys else None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "calls"):
            setattr(self, f, getattr(self, f) + getattr(other, f))

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Usage":
        return cls(**{k: int(d.get(k, 0)) for k in cls().__dict__})

    def estimated_cost(self, model: str) -> float | None:
        price = price_for(model)
        if price is None:
            return None
        pin, pout, pcache = price
        pcache = pin if pcache is None else pcache
        return round(
            (self.input_tokens * pin + self.cache_read_tokens * pcache
             + self.cache_write_tokens * pin * CACHE_WRITE_FACTOR + self.output_tokens * pout) / 1e6, 6)


def estimate_cost(model: str, input_chars: int, max_tokens: int) -> float:
    """Pre-call worst-case estimate: input tokens ~ chars/4 (no cache discount) plus `max_tokens` of output. 0 if unpriced."""
    price = price_for(model)
    if price is None:
        return 0.0
    return (input_chars / 4 * price[0] + max_tokens * price[1]) / 1e6


@dataclass
class CallResult:
    """`parsed` is None when `error` is set (refusal, ...). Callers must check `ok` and never invent a result."""
    parsed: BaseModel | None
    usage: Usage
    served_model: str
    error: str | None = None
    refused: bool = False

    @property
    def ok(self) -> bool:
        return self.parsed is not None and self.error is None


class StructuredLLM(Protocol):
    model: str

    async def call(self, *, prefix: str, user: str, schema: type[BaseModel], max_tokens: int = 4096,
                   effort: Effort = "high", purpose: str = "", budget: Any | None = None,
                   hint: dict | None = None) -> CallResult: ...

    def with_model(self, model: str) -> "StructuredLLM": ...


class RequestShapeError(Exception):
    """The request would be rejected by the API (e.g. forced tool_choice). Never retried."""


async def with_retries(fn: Callable[[], Awaitable[T]], *, attempts: int = 4, base_delay: float = 1.0,
                       retry_on: tuple[type[BaseException], ...] = (Exception,),
                       no_retry: tuple[type[BaseException], ...] = ()) -> T:
    """Exponential backoff. base_delay=0 disables sleeping (tests)."""
    for i in range(attempts):
        try:
            return await fn()
        except no_retry:
            raise
        except retry_on:
            if i == attempts - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** i))
    raise AssertionError("unreachable")


def json_schema_for(schema: type[BaseModel]) -> dict:
    """Pydantic schema transformed to the subset structured outputs support (additionalProperties:false, etc.)."""
    try:
        from anthropic import transform_schema

        return transform_schema(schema.model_json_schema())
    except ImportError:  # very old SDK
        return schema.model_json_schema()


def _usage_of(resp: Any) -> Usage:
    u = resp.usage
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        calls=1)


class AnthropicLLM:
    """Messages API with JSON-schema structured output; the shared prefix is cached (cache_control ephemeral)."""

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL, *, client: Any | None = None,
                 retry_delay: float = 1.0, attempts: int = 4):
        if client is None:
            import anthropic

            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            client = anthropic.AsyncAnthropic()
        self.client, self.model = client, model
        self.retry_delay, self.attempts = retry_delay, attempts

    def with_model(self, model: str) -> "AnthropicLLM":
        other = copy.copy(self)
        other.model = model
        return other

    def build_request(self, *, prefix: str, user: str, schema: type[BaseModel], max_tokens: int,
                      effort: Effort) -> dict:
        # No tool_choice, temperature, top_p, top_k or thinking (see module docstring).
        return dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": json_schema_for(schema)}},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    async def _create(self, request: dict, hint: dict | None) -> Any:
        return await self.client.beta.messages.create(**request)

    async def call(self, *, prefix: str, user: str, schema: type[BaseModel], max_tokens: int = 4096,
                   effort: Effort = "high", purpose: str = "", budget: Any | None = None,
                   hint: dict | None = None) -> CallResult:
        """Raises `BudgetExceeded` (before any API call) if `budget` cannot cover the worst-case cost."""
        request = self.build_request(prefix=prefix, user=user, schema=schema, max_tokens=max_tokens, effort=effort)
        reserved = budget.reserve(self.model, len(prefix) + len(user), max_tokens, purpose) if budget else 0.0
        total = Usage()
        served = self.model

        async def once() -> CallResult:
            nonlocal served
            resp = await self._create(request, hint)
            usage = _usage_of(resp)
            total.add(usage)
            served = getattr(resp, "model", None) or self.model
            if budget:
                budget.record(purpose, served if price_for(served) else self.model, usage)
            # Refusal check comes BEFORE touching content.
            if getattr(resp, "stop_reason", None) == "refusal":
                return CallResult(None, usage, served, error="refusal: the model declined to answer (stop_reason=refusal)",
                                  refused=True)
            if getattr(resp, "stop_reason", None) == "max_tokens":
                raise ValueError("response truncated at max_tokens")
            text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
            try:
                parsed = schema.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as e:  # retried: sampling may fix it
                raise ValueError(f"structured output failed validation: {e}") from e
            return CallResult(parsed, usage, served)

        try:
            res = await with_retries(once, attempts=self.attempts, base_delay=self.retry_delay,
                                     no_retry=(RequestShapeError,))
        finally:
            if budget:
                budget.release(reserved)
        res.usage = total  # all attempts, including retried ones
        return res


@dataclass
class UsageTracker:
    """Accumulates usage by (role, model)."""
    by_key: dict[tuple[str, str], Usage] = field(default_factory=dict)

    def add(self, role: str, model: str, usage: Usage) -> None:
        self.by_key.setdefault((role, model), Usage()).add(usage)

    def summary(self) -> list[dict]:
        return [{"role": r, "model": m, **u.to_dict(), "estimated_cost_usd": u.estimated_cost(m)}
                for (r, m), u in sorted(self.by_key.items())]
