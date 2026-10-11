"""Pluggable text-generation providers for study cells.

Every cell is ONE independent request built from scratch (`GenRequest`): no conversation object exists to share, so cell
isolation is structural. Providers:

- `AnthropicProvider`: the house wrapper (`llm.AnthropicLLM`) with a text-output request: explicit `output_config.effort`, no
  forced `tool_choice`, no temperature/top_p/top_k/thinking, and the server-side fallback beta, exactly as llm.py does for
  structured output. `stop_reason == "refusal"` is checked before content is read.
- `OpenAIResponsesProvider`: `POST /v1/responses` with `model` and `input`. The wire call is isolated in `post_responses`.
- `GoogleProvider`: stub. Implement `generate` and register it with `register_provider`.
- `FakeProvider`: offline, deterministic (`--dry-run` and tests).

All real providers are budget-gated through `essay_grader.budget.Budget` (worst case reserved before the call, actual usage
recorded after). A model with no known price cannot be gated: the runner refuses a finite budget in that case.
"""
from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from ..budget import Budget
from ..llm import FALLBACK_BETA, AnthropicLLM, RequestShapeError, Usage, price_for, with_retries

# Text output: the request sets only `output_config.effort`; text is the default output format. If the API wants an explicit
# text format object, set it here (e.g. {"type": "text"}); the fake request validator accepts either.  # VERIFY live
TEXT_OUTPUT_FORMAT: dict | None = None


@dataclass
class GenRequest:
    cell_id: str
    model: str
    system: str  # arm materials block ("" for arm A)
    user: str  # rendered protocol prompt incl. the exam question
    max_tokens: int
    effort: str = "high"
    purpose: str = ""
    hint: dict = field(default_factory=dict)  # for fakes only; never sent

    def in_chars(self) -> int:
        return len(self.system) + len(self.user)

    def request_sha256(self) -> str:
        return hashlib.sha256((self.system + "\n\x00\n" + self.user).encode("utf-8")).hexdigest()


@dataclass
class GenResult:
    text: str | None
    served_model: str
    usage: Usage
    stop_reason: str | None = None
    truncated: bool = False
    error: str | None = None
    refused: bool = False

    @property
    def ok(self) -> bool:
        return self.text is not None and self.error is None


class Provider(Protocol):
    name: str

    async def generate(self, req: GenRequest, *, budget: Budget | None = None) -> GenResult: ...


# ---------------------------------------------------------------------------------------------------------------
# budget gating shared by non-Anthropic providers (the Anthropic wrapper gates inside its own loop, like llm.call)
# ---------------------------------------------------------------------------------------------------------------
async def gated(budget: Budget | None, req: GenRequest, do_call: Callable[[], Awaitable[GenResult]]) -> GenResult:
    reserved = budget.reserve(req.model, req.in_chars(), req.max_tokens, req.purpose) if budget else 0.0
    try:
        res = await do_call()
        if budget:
            budget.record(req.purpose, res.served_model if price_for(res.served_model) else req.model, res.usage)
        return res
    finally:
        if budget:
            budget.release(reserved)


# ---------------------------------------------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------------------------------------------
class AnthropicTextLLM(AnthropicLLM):
    """`AnthropicLLM` plus a free-text call. Reuses its client, retries, usage accounting and budget protocol.

    Server-side model fallback is OFF by default for study cells: a cell answered by a different model than the
    design names would confound the model comparison. A refusal is recorded as data instead. Set
    `allow_model_fallback=True` only if the study design explicitly accepts substituted cells.
    """

    allow_model_fallback: bool = False

    def build_text_request(self, *, system: str, user: str, max_tokens: int, effort: str) -> dict:
        oc: dict[str, Any] = {"effort": effort}
        if TEXT_OUTPUT_FORMAT:
            oc["format"] = TEXT_OUTPUT_FORMAT
        req: dict[str, Any] = dict(
            model=self.model, max_tokens=max_tokens, messages=[{"role": "user", "content": user}],
            output_config=oc)
        if self.allow_model_fallback:
            req.update(betas=[FALLBACK_BETA], fallbacks="default")
        if system:
            req["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        return req

    async def generate_text(self, req: GenRequest, *, budget: Budget | None = None) -> GenResult:
        from ..llm import _usage_of

        request = self.build_text_request(system=req.system, user=req.user, max_tokens=req.max_tokens, effort=req.effort)
        reserved = budget.reserve(self.model, req.in_chars(), req.max_tokens, req.purpose) if budget else 0.0
        total = Usage()
        served = self.model

        async def once() -> GenResult:
            nonlocal served
            resp = await self._create(request, req.hint)
            usage = _usage_of(resp)
            total.add(usage)
            served = getattr(resp, "model", None) or self.model
            if budget:
                budget.record(req.purpose, served if price_for(served) else self.model, usage)
            stop = getattr(resp, "stop_reason", None)
            if stop == "refusal":  # BEFORE touching content
                return GenResult(None, served, usage, stop, error="refusal: the model declined to answer", refused=True)
            text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
            return GenResult(text, served, usage, stop, truncated=(stop == "max_tokens"))

        try:
            res = await with_retries(once, attempts=self.attempts, base_delay=self.retry_delay, no_retry=(RequestShapeError,))
        finally:
            if budget:
                budget.release(reserved)
        res.usage = total
        return res


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, llm: AnthropicTextLLM | None = None):
        self._llm = llm

    def _client(self) -> AnthropicTextLLM:
        if self._llm is None:
            self._llm = AnthropicTextLLM()
        return self._llm

    async def generate(self, req: GenRequest, *, budget: Budget | None = None) -> GenResult:
        return await self._client().with_model(req.model).generate_text(req, budget=budget)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------------------------------
# OpenAI Responses API
# ---------------------------------------------------------------------------------------------------------------
async def post_responses(client: Any, body: dict) -> dict:
    """The ONLY place that talks to the OpenAI Responses API.  # VERIFY against OpenAI Responses docs

    Assumed shape (check before the first live run): POST /v1/responses with {model, input, max_output_tokens};
    response {id, model, status, incomplete_details, output:[{type:"message", content:[{type:"output_text", text}|
    {type:"refusal", refusal}]}, ...], usage:{input_tokens, output_tokens, input_tokens_details:{cached_tokens}}}.
    `input` is sent as one user message with an `input_text` part (same part type the Decisions adapter uses).
    """
    create = getattr(getattr(client, "responses", None), "create", None)
    if create is not None:
        return _to_dict(await create(**body))
    return _to_dict(await client.post("/responses", body=body, cast_to=object))


def _to_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    for m in ("model_dump", "to_dict"):
        if hasattr(obj, m):
            return getattr(obj, m)()
    raise TypeError(f"cannot convert {type(obj).__name__} to dict")


def build_responses_body(req: GenRequest) -> dict:
    text = f"{req.system}\n\n{req.user}" if req.system else req.user
    return {"model": req.model,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
            "max_output_tokens": req.max_tokens}


def parse_responses(resp: dict, requested_model: str) -> GenResult:  # VERIFY against OpenAI Responses docs
    u = resp.get("usage") or {}
    cached = int(((u.get("input_tokens_details") or {}).get("cached_tokens")) or 0)
    usage = Usage(input_tokens=max(0, int(u.get("input_tokens", 0) or 0) - cached), output_tokens=int(u.get("output_tokens", 0) or 0),
                  cache_read_tokens=cached, calls=1)
    served = resp.get("model") or requested_model
    texts: list[str] = []
    refused = False
    for item in resp.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "refusal":
                refused = True
    status = resp.get("status")
    inc = (resp.get("incomplete_details") or {}).get("reason")
    if refused:
        return GenResult(None, served, usage, status, error="refusal: the model declined to answer", refused=True)
    if status == "failed" or resp.get("error"):
        raise ValueError(f"responses API reported failure: {resp.get('error')!r}")
    return GenResult("".join(texts), served, usage, status, truncated=(status == "incomplete" and inc == "max_output_tokens"))


class OpenAIResponsesProvider:
    name = "openai"

    def __init__(self, transport: Callable[[dict], Awaitable[dict]] | None = None, *, retry_delay: float = 1.0, attempts: int = 4):
        self._transport = transport
        self.retry_delay, self.attempts = retry_delay, attempts

    def _post(self) -> Callable[[dict], Awaitable[dict]]:
        if self._transport is None:
            import openai

            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is not set")
            client = openai.AsyncOpenAI()

            async def post(body: dict) -> dict:
                return await post_responses(client, body)

            self._transport = post
        return self._transport

    async def generate(self, req: GenRequest, *, budget: Budget | None = None) -> GenResult:
        body = build_responses_body(req)
        post = self._post()

        async def do() -> GenResult:
            return await with_retries(lambda: _once(post, body, req.model), attempts=self.attempts, base_delay=self.retry_delay)

        return await gated(budget, req, do)


async def _once(post, body, model) -> GenResult:
    return parse_responses(await post(body), model)


# ---------------------------------------------------------------------------------------------------------------
# Google (stub) and the registry
# ---------------------------------------------------------------------------------------------------------------
class GoogleProvider:
    """Stub for a third provider. To add Google (or anyone else): implement `generate(req, budget=)` returning a `GenResult`
    (one independent request, budget gated with `gated()`), then `register_provider("google", lambda dry: GoogleProvider())`."""

    name = "google"

    async def generate(self, req: GenRequest, *, budget: Budget | None = None) -> GenResult:
        raise NotImplementedError("GoogleProvider is a stub. See the class docstring and docs/STUDY.md (adding a provider).")


def canned_text(req: GenRequest) -> str:
    """Deterministic, protocol-compliant fake output: ~90% of the ceiling for essays, letters for MCQ."""
    h = req.hint
    seed = int(hashlib.sha256(req.cell_id.encode()).hexdigest()[:8], 16)
    if h.get("kind") == "mcq":
        n = int(h.get("n_questions") or 1)
        return "\n".join(f"{i}. {'ABCD'[(seed >> i) % 4]}" for i in range(1, n + 1)) + "\n"
    target = int(0.9 * h["word_ceiling"]) if h.get("word_ceiling") else 120
    vocab = ["the", "rule", "here", "applies", "because", "defendant", "plaintiff", "fails", "on", "these", "facts", "court",
             "would", "hold", "that", "claim", "likely", "succeeds", "element", "met"]
    words = [vocab[(seed + i * 7) % len(vocab)] for i in range(target)]
    sent = [" ".join(words[i:i + 12]).capitalize() + "." for i in range(0, len(words), 12)]
    return "\n\n".join(" ".join(sent[i:i + 4]) for i in range(0, len(sent), 4))


class FakeProvider:
    """Offline stand-in for any provider. `script` maps cell_id (or a callable of GenRequest) to the exact text to return."""

    def __init__(self, name: str = "fake", *, script: dict[str, str] | Callable[[GenRequest], str] | None = None,
                 served_model: str | None = None, refuse: set[str] | None = None, fail: set[str] | None = None):
        self.name = name
        self.script, self.served_model = script, served_model
        self.refuse, self.fail = set(refuse or ()), set(fail or ())
        self.requests: list[GenRequest] = []

    async def generate(self, req: GenRequest, *, budget: Budget | None = None) -> GenResult:
        async def do() -> GenResult:
            self.requests.append(req)
            if req.cell_id in self.fail:
                raise RuntimeError("fake transport failure")
            served = self.served_model or req.model
            in_tok = req.in_chars() // 4
            if req.cell_id in self.refuse:
                return GenResult(None, served, Usage(input_tokens=in_tok, calls=1), "refusal", error="refusal: fake", refused=True)
            if callable(self.script):
                text = self.script(req)
            elif self.script and req.cell_id in self.script:
                text = self.script[req.cell_id]
            else:
                text = canned_text(req)
            out_tok = min(req.max_tokens, math.ceil(len(text.split()) * 1.4))
            return GenResult(text, served, Usage(input_tokens=in_tok, output_tokens=out_tok, calls=1), "end_turn")

        return await gated(budget, req, do)


PROVIDER_FACTORIES: dict[str, Callable[[bool], Provider]] = {}


def register_provider(name: str, factory: Callable[[bool], Provider]) -> None:
    """factory(dry_run) -> Provider. Dry-run factories must return an offline fake."""
    PROVIDER_FACTORIES[name] = factory


register_provider("anthropic", lambda dry: FakeProvider("anthropic") if dry else AnthropicProvider())
register_provider("openai", lambda dry: FakeProvider("openai") if dry else OpenAIResponsesProvider())
register_provider("google", lambda dry: FakeProvider("google") if dry else GoogleProvider())


def make_provider(name: str, *, dry_run: bool = False) -> Provider:
    if name not in PROVIDER_FACTORIES:
        raise KeyError(f"unknown provider {name!r}; known: {sorted(PROVIDER_FACTORIES)}")
    return PROVIDER_FACTORIES[name](dry_run)
