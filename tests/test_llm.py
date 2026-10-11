"""Claude API request shape, refusal handling, prices/cost."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from essay_grader.fake import FakeLLM, validate_request
from essay_grader.graders.claude_grader import ClaudeGrader, GradeOutput
from essay_grader.llm import (FALLBACK_BETA, PRICE_PER_MTOK, AnthropicLLM, RequestShapeError, Usage, price_for)

FORBIDDEN = ("tool_choice", "tools", "temperature", "top_p", "top_k", "thinking")


class _Resp:
    def __init__(self, *, text="", stop_reason="end_turn", model="claude-sonnet-5-5-20260101", usage=None, guard=False):
        self._content, self.guard = [NS(type="text", text=text)], guard
        self.stop_reason, self.model = stop_reason, model
        self.usage = usage or NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)

    @property
    def content(self):
        if self.guard:
            raise AssertionError("content read before checking stop_reason")
        return self._content


class _Messages:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, []

    async def create(self, **kw):
        self.kwargs.append(kw)
        return self.resp


def _llm(resp, model="claude-sonnet-5-5"):
    m = _Messages(resp)
    return AnthropicLLM(model, client=NS(beta=NS(messages=m)), retry_delay=0, attempts=2), m


def _call(llm, **kw):
    return asyncio.run(llm.call(prefix="P", user="U", schema=GradeOutput, **kw))


def test_request_shape_has_no_forced_tool_choice_and_uses_output_config():
    llm, m = _llm(_Resp(text=json.dumps({"judgments": []})))
    res = _call(llm, effort="medium")
    k = m.kwargs[0]
    for bad in FORBIDDEN:
        assert bad not in k
    fmt = k["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["type"] == "object" and "judgments" in fmt["schema"]["properties"]
    assert k["output_config"]["effort"] == "medium"
    assert k["betas"] == [FALLBACK_BETA] == ["server-side-fallback-2026-07-01"] and k["fallbacks"] == "default"
    assert k["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert res.ok and res.parsed.judgments == [] and res.served_model.startswith("claude-sonnet-5-5-")
    validate_request(k)  # the offline API-rule validator accepts exactly what the real wrapper sends


def test_validator_rejects_forced_tool_choice_and_sampling_params():
    base = AnthropicLLM("claude-opus-5-5", client=object()).build_request(prefix="p", user="u", schema=GradeOutput, max_tokens=10, effort="high")
    validate_request(base)
    for bad in ({"tool_choice": {"type": "tool", "name": "x"}}, {"tool_choice": {"type": "any"}}, {"temperature": 0},
                {"top_p": 1}, {"top_k": 5}, {"thinking": {"type": "disabled"}}):
        with pytest.raises(RequestShapeError):
            validate_request({**base, **bad})
    with pytest.raises(RequestShapeError):
        validate_request({k: v for k, v in base.items() if k != "output_config"})


def test_refusal_returns_structured_error_and_never_reads_content():
    llm, m = _llm(_Resp(stop_reason="refusal", guard=True))
    res = _call(llm)
    assert res.refused and not res.ok and res.parsed is None and res.error.startswith("refusal")
    assert len(m.kwargs) == 1  # not retried


def test_refusal_never_fabricates_a_grade(locked):
    llm = FakeLLM(refuse_kinds={"grade"})
    r = asyncio.run(ClaudeGrader(llm, retry_delay=0).grade(locked, locked.question("Q1"), "exam", "some answer"))
    assert r.error and r.error.startswith("refusal") and r.grades == []


def test_bad_json_is_retried_then_raises():
    llm, m = _llm(_Resp(text="not json"))
    with pytest.raises(ValueError):
        _call(llm)
    assert len(m.kwargs) == 2


def test_truncation_is_an_error_not_a_grade():
    llm, _ = _llm(_Resp(text='{"judgments": [', stop_reason="max_tokens"))
    with pytest.raises(ValueError):
        _call(llm)


def test_every_fake_request_has_expected_shape_and_effort():
    from essay_grader.harden import HardenConfig, harden
    import essay_grader.fake as fake

    seen = []
    orig = fake.FakeLLM._create

    async def spy(self, request, hint):
        seen.append((hint["kind"], request["output_config"]["effort"], request))
        return await orig(self, request, hint)

    fake.FakeLLM._create = spy
    try:
        from essay_grader.rubric import load_rubric
        from pathlib import Path

        fix = Path(__file__).parent / "fixtures"
        asyncio.run(harden(load_rubric(fix / "rubric.yaml"), (fix / "exam.txt").read_text(), "syl", FakeLLM(), cfg=HardenConfig(use_decisions=False)))
    finally:
        fake.FakeLLM._create = orig
    eff = {kind: e for kind, e, _ in seen}
    assert eff == {"critique": "high", "synth_all": "medium", "grade": "high", "questionnaire": "high"}
    for _, _, req in seen:
        validate_request(req)
        assert "tool_choice" not in req and req["output_config"]["format"]["type"] == "json_schema"


def test_prices_and_cost_from_usage():
    assert PRICE_PER_MTOK["claude-opus-5-5"] == (4.00, 20.00, 0.20)
    assert PRICE_PER_MTOK["claude-sonnet-5-5"] == (2.00, 10.00, 0.20)
    assert PRICE_PER_MTOK["claude-haiku-5-5"] == (0.10, 0.50, None)
    assert PRICE_PER_MTOK["gpt-6-luna"] == (0.10, 0.0, None)
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert u.estimated_cost("claude-opus-5-5") == pytest.approx(4 + 20 + 0.2 + 5.0)  # cache write = 1.25x input
    assert u.estimated_cost("claude-sonnet-5-5") == pytest.approx(2 + 10 + 0.2 + 2.5)
    assert u.estimated_cost("claude-haiku-5-5") == pytest.approx(0.1 + 0.5 + 0.1 + 0.125)  # no cache discount listed
    assert Usage(input_tokens=1_000_000).estimated_cost("gpt-6-luna") == pytest.approx(0.10)
    assert Usage(input_tokens=5).estimated_cost("mystery-model") is None
    assert price_for("claude-opus-5-5-20260101") == PRICE_PER_MTOK["claude-opus-5-5"]
