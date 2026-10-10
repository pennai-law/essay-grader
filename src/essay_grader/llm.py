"""Anthropic structured-output wrapper (forced single tool + pydantic validation), usage/cost, retries."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
T = TypeVar("T")

# ESTIMATES ONLY, USD per million tokens (input, output). Placeholders: update from the provider's
# price page before relying on cost figures. Unknown models report cost as null.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (5.0, 25.0),
    "claude-sonnet-5-5": (3.0, 15.0),
    "gpt-6-luna": (1.0, 4.0),
}
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


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
        price = PRICE_PER_MTOK.get(model)
        if price is None:
            return None
        pin, pout = price
        return round(
            (self.input_tokens * pin + self.cache_read_tokens * pin * CACHE_READ_FACTOR
             + self.cache_write_tokens * pin * CACHE_WRITE_FACTOR + self.output_tokens * pout) / 1e6, 4)


@dataclass
class CallResult:
    parsed: BaseModel
    usage: Usage
    served_model: str


class StructuredLLM(Protocol):
    model: str

    async def call(self, *, prefix: str, user: str, tool_name: str, tool_description: str,
                   schema: type[BaseModel], max_tokens: int = 4096, hint: dict | None = None) -> CallResult: ...


async def with_retries(fn: Callable[[], Awaitable[T]], *, attempts: int = 4, base_delay: float = 1.0,
                       retry_on: tuple[type[BaseException], ...] = (Exception,)) -> T:
    """Exponential backoff. base_delay=0 disables sleeping (tests)."""
    for i in range(attempts):
        try:
            return await fn()
        except retry_on:
            if i == attempts - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** i))
    raise AssertionError("unreachable")


class AnthropicLLM:
    """Messages API with one forced tool; shared prefix is cached (cache_control ephemeral)."""

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL, *, client: Any | None = None,
                 retry_delay: float = 1.0, attempts: int = 4):
        if client is None:
            import anthropic

            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            client = anthropic.AsyncAnthropic()
        self.client, self.model = client, model
        self.retry_delay, self.attempts = retry_delay, attempts

    async def call(self, *, prefix: str, user: str, tool_name: str, tool_description: str,
                   schema: type[BaseModel], max_tokens: int = 4096, hint: dict | None = None) -> CallResult:
        # NOTE: temperature intentionally left at the API default.
        async def once() -> CallResult:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}],
                tools=[{"name": tool_name, "description": tool_description,
                        "input_schema": schema.model_json_schema()}],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": user}],
            )
            if getattr(resp, "stop_reason", None) == "max_tokens":
                raise ValueError("response truncated at max_tokens")
            block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
            if block is None:
                raise ValueError("model did not return the forced tool call")
            try:
                parsed = schema.model_validate(block.input)
            except ValidationError as e:  # retried: sampling may fix it
                raise ValueError(f"tool output failed validation: {e}") from e
            u = resp.usage
            usage = Usage(
                input_tokens=getattr(u, "input_tokens", 0) or 0,
                output_tokens=getattr(u, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                calls=1,
            )
            return CallResult(parsed, usage, getattr(resp, "model", self.model))

        return await with_retries(once, attempts=self.attempts, base_delay=self.retry_delay)


@dataclass
class UsageTracker:
    """Accumulates usage by (role, model)."""
    by_key: dict[tuple[str, str], Usage] = field(default_factory=dict)

    def add(self, role: str, model: str, usage: Usage) -> None:
        self.by_key.setdefault((role, model), Usage()).add(usage)

    def summary(self) -> list[dict]:
        return [{"role": r, "model": m, **u.to_dict(), "estimated_cost_usd": u.estimated_cost(m)}
                for (r, m), u in sorted(self.by_key.items())]
