"""Cell runner: isolation, verbatim capture + metadata, resume, budget, concurrency; provider request shapes."""
import asyncio
import hashlib
import json
import os
import stat
from types import SimpleNamespace as NS

import pytest

from essay_grader.budget import Budget
from essay_grader.llm import FALLBACK_BETA, Usage
from essay_grader.study.design import Cell, Design
from essay_grader.study.providers import (AnthropicProvider, AnthropicTextLLM, FakeProvider, GenRequest, GoogleProvider,
                                          OpenAIResponsesProvider, build_responses_body, make_provider, parse_responses)
from essay_grader.study.runner import UnpricedModelError, register_prices
from essay_grader.study.store import CaptureTamperedError, StaleCaptureError, StudyStore
from test_study_helpers import (CLEAN_ESSAY, CLEAN_MCQ, clean_script, design, design_path, make_study, multi_model, protocol,  # noqa: F401
                                run, store)

OUTLINE_MARK = "Outline: Introductory Widget Law"  # appears only in arm B's outline.md
SYLLABUS_MARK = "Casebook: Widgets and Wares"  # in arm S and arm B syllabus.md


# ---- isolation -------------------------------------------------------------------------------------------------
def test_each_cell_is_one_independent_call_containing_only_its_arms_materials(design, store):
    summary, providers = run(design, store)
    reqs = providers["anthropic"].requests
    assert summary.status == "complete" and len(reqs) == len(design.cells()) == 6
    assert len({r.cell_id for r in reqs}) == 6  # one call per cell, no cell called twice, no batching
    for r in reqs:
        cell = Cell.parse(r.cell_id)
        if cell.arm == "A":
            assert r.system == "" and SYLLABUS_MARK not in r.user and OUTLINE_MARK not in r.user
        elif cell.arm == "S":
            assert SYLLABUS_MARK in r.system and OUTLINE_MARK not in r.system  # no B material leaks into S
        else:
            assert SYLLABUS_MARK in r.system and OUTLINE_MARK in r.system
        # nothing from another cell's prompt: the user text is exactly this component's rendered prompt
        comp_q = design.question_text(design.component(cell.component)).strip()
        assert comp_q in r.user
        for other in design.components:
            if other.id != cell.component:
                assert design.question_text(other).strip().splitlines()[2 if other.kind == "mcq" else 0][:30] not in r.user
    # prompt is identical across arms for a component (arms differ ONLY in materials)
    by_comp = {}
    for r in reqs:
        by_comp.setdefault(Cell.parse(r.cell_id).component, set()).add(r.user)
    assert all(len(v) == 1 for v in by_comp.values())


def test_materials_are_loaded_through_the_sandbox_only(design, store, monkeypatch):
    import essay_grader.study.runner as runner_mod
    from essay_grader.study import materials

    seen = []
    real = materials.ArmSandbox.load

    def spy(self):
        seen.append((self.arm_id, self.root))
        return real(self)

    monkeypatch.setattr(materials.ArmSandbox, "load", spy)
    run(design, store)
    assert {a for a, _ in seen} == {"A", "S", "B"}
    assert {a: r.name for a, r in seen if r} == {"S": "syllabus", "B": "full"}


# ---- verbatim capture ------------------------------------------------------------------------------------------
def test_capture_is_byte_exact_and_read_only_with_request_metadata(design, store):
    weird = "  Course materials are attached — here.\r\nSecond line with trailing space   \n\n\nEnd without newline"
    fake = FakeProvider("anthropic", script=lambda r: weird, served_model="claude-sonnet-5-5-20261001")
    summary, _ = run(design, store, providers={"anthropic": fake})
    cell = Cell("q1", "B", "sonnet")
    path = store.capture_path(cell)
    assert path.read_bytes() == weird.encode("utf-8")  # exactly as returned: CRLF, em dash, trailing spaces, no final newline
    assert not os.access(path, os.W_OK) or stat.S_IMODE(path.stat().st_mode) & stat.S_IWUSR == 0
    m = store.read_meta(cell)
    assert m["requested_model"] == "claude-sonnet-5-5" and m["served_model"] == "claude-sonnet-5-5-20261001"
    assert m["model_fallback"] is False  # a dated snapshot of the requested model is not a fallback
    assert m["capture_sha256"] == hashlib.sha256(weird.encode()).hexdigest()
    assert m["prompt_sha256"] == hashlib.sha256(store.prompt_path(cell).read_text().encode()).hexdigest()
    assert m["prompt_version"] == 2 and set(m["template_sha256"]) == {"essay", "mcq"}
    assert [f["path"] for f in m["materials"]["manifest"]] == ["outline.md", "syllabus.md"]
    assert all(len(f["sha256"]) == 64 for f in m["materials"]["manifest"])
    for k in ("started_at", "finished_at", "usage", "estimated_cost_usd", "effort", "max_tokens", "examid", "request_sha256", "provider"):
        assert m[k] not in (None, "")
    assert m["examid"] == "0891" and m["effort"] == "medium" and m["usage"]["output_tokens"] > 0
    assert store.read_meta(Cell("q1", "A", "sonnet"))["materials"]["manifest"] == []


def test_overwriting_or_tampering_with_a_capture_is_detected(design, store):
    run(design, store)
    cell = Cell("q1", "A", "sonnet")
    with pytest.raises(FileExistsError):
        store.write_capture(cell, "new text", {})
    p = store.capture_path(cell)
    os.chmod(p, 0o644)
    p.write_text("edited")
    with pytest.raises(CaptureTamperedError):
        store.read_capture(cell)


def test_model_fallback_is_flagged(design, store):
    fake = FakeProvider("anthropic", script=clean_script, served_model="claude-haiku-5-5")
    summary, _ = run(design, store, providers={"anthropic": fake})
    assert len(summary.served_model_differs) == 6 and store.read_meta(Cell("q1", "A", "sonnet"))["model_fallback"] is True


# ---- resume ----------------------------------------------------------------------------------------------------
def test_resume_skips_captured_cells_and_retries_failed_ones(design, store):
    flaky = FakeProvider("anthropic", script=clean_script, fail={"q1__S__sonnet"}, refuse={"mcq__B__sonnet"})
    s1, _ = run(design, store, providers={"anthropic": flaky})
    assert sorted(s1.failed) == ["mcq__B__sonnet", "q1__S__sonnet"] and len(s1.captured) == 4
    assert store.error_path(Cell("q1", "S", "sonnet")).is_file() and not store.capture_path(Cell("q1", "S", "sonnet")).exists()

    healthy = FakeProvider("anthropic", script=clean_script)
    s2, _ = run(design, store, providers={"anthropic": healthy})
    assert sorted(r.cell_id for r in healthy.requests) == ["mcq__B__sonnet", "q1__S__sonnet"]  # ONLY the missing cells are called
    assert len(s2.resumed_skipped) == 4 and s2.status == "complete"
    assert not store.error_path(Cell("q1", "S", "sonnet")).exists()

    third = FakeProvider("anthropic", script=clean_script)
    s3, _ = run(design, store, providers={"anthropic": third})
    assert third.requests == [] and len(s3.resumed_skipped) == 6


def test_resume_refuses_when_inputs_changed_since_capture(design, store):
    run(design, store)
    (design.path("materials/syllabus") / "syllabus.md").write_text("# Changed syllabus\n")
    fresh = FakeProvider("anthropic", script=clean_script)
    with pytest.raises(StaleCaptureError, match="materials manifest changed"):
        run(design, store, providers={"anthropic": fresh})
    assert fresh.requests == []


def test_resume_refuses_when_the_model_changed(design_path, tmp_path, store):
    d = Design.load(design_path)
    run(d, store)
    d.models[0].model = "claude-opus-5-5"
    with pytest.raises(StaleCaptureError, match="model changed"):
        run(d, store)


# ---- budget ----------------------------------------------------------------------------------------------------
def test_budget_is_enforced_before_any_call(design, store):
    b = Budget(max_usd=0.0)
    summary, providers = run(design, store, budget=b, concurrency=1)
    assert providers["anthropic"].requests == []  # nothing was sent
    assert summary.status == "budget_exhausted" and len(summary.skipped_budget) == 6 and b.spent_usd == 0.0
    assert store.captured_cells() == []


def test_budget_stops_cleanly_midway_and_resume_finishes(design, store):
    # the ledger holds roughly two cells of actual cost; a third worst-case reservation must be refused
    worst_case = Budget.estimate("claude-sonnet-5-5", 5000, design.max_tokens_for(design.component("q1"), design.model("sonnet")))
    b = Budget(max_usd=worst_case + 0.004)
    s1, p1 = run(design, store, budget=b, concurrency=1)
    assert s1.status == "budget_exhausted" and 0 < len(s1.captured) < 6
    assert b.spent_usd <= b.max_usd and b.reserved_usd == 0
    assert any(e.purpose.startswith("study:") for e in b.ledger)
    s2, _ = run(design, store, budget=Budget(max_usd=5.0))
    assert s2.status == "complete" and len(store.captured_cells()) == 6
    assert len(s2.resumed_skipped) == len(s1.captured)


def test_unpriced_model_cannot_run_under_a_finite_budget(tmp_path):
    d = Design.load(make_study(tmp_path, lambda d: d["models"][0].update(model="brand-new-model-1")))
    st = StudyStore(tmp_path / "out", d)
    with pytest.raises(UnpricedModelError, match="brand-new-model-1"):
        run(d, st, budget=Budget(max_usd=1.0))
    d2 = Design.load(make_study(tmp_path / "b", lambda d: d["models"][0].update(model="brand-new-model-1", price_per_mtok=[3, 15, 0.3])))
    assert register_prices(d2) == []
    s, _ = run(d2, StudyStore(tmp_path / "out2", d2), budget=Budget(max_usd=5.0))
    assert s.status == "complete" and s.spent_usd > 0
    from essay_grader.llm import PRICE_PER_MTOK

    PRICE_PER_MTOK.pop("brand-new-model-1")


def test_concurrency_is_bounded_by_the_semaphore(design, store):
    state = {"cur": 0, "max": 0}

    class Slow(FakeProvider):
        async def generate(self, req, *, budget=None):
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            await asyncio.sleep(0.01)
            try:
                return await super().generate(req, budget=budget)
            finally:
                state["cur"] -= 1

    run(design, store, providers={"anthropic": Slow("anthropic", script=clean_script)}, concurrency=2)
    assert state["max"] == 2


def test_multi_model_design_runs_each_provider_once_per_cell(tmp_path):
    d = Design.load(make_study(tmp_path, multi_model))
    st = StudyStore(tmp_path / "out", d)
    provs = {"anthropic": FakeProvider("anthropic", script=clean_script), "openai": FakeProvider("openai", script=clean_script)}
    s, _ = run(d, st, providers=provs)
    assert s.status == "complete" and len(provs["anthropic"].requests) == 6 and len(provs["openai"].requests) == 6
    assert st.read_meta(Cell("mcq", "S", "gpt"))["examid"] == "0111"


# ---- providers: request shapes ---------------------------------------------------------------------------------
class _Resp:
    def __init__(self, text="hello", stop="end_turn", model="claude-sonnet-5-5-20261001", guard=False):
        self._c, self.guard = [NS(type="text", text=text)], guard
        self.stop_reason, self.model = stop, model
        self.usage = NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)

    @property
    def content(self):
        if self.guard:
            raise AssertionError("content read before checking stop_reason")
        return self._c


class _Msgs:
    def __init__(self, resp):
        self.resp, self.kw = resp, []

    async def create(self, **kw):
        self.kw.append(kw)
        return self.resp


def _anthropic(resp):
    m = _Msgs(resp)
    return AnthropicProvider(AnthropicTextLLM("claude-sonnet-5-5", client=NS(beta=NS(messages=m)), retry_delay=0, attempts=2)), m


def _req(**kw):
    base = dict(cell_id="q1__B__sonnet", model="claude-sonnet-5-5", system="MATERIALS", user="PROMPT", max_tokens=2000, effort="medium",
                purpose="study:q1__B__sonnet")
    return GenRequest(**{**base, **kw})


def test_anthropic_text_request_has_no_forced_tool_uses_effort_and_no_model_fallback():
    prov, m = _anthropic(_Resp(text="the answer"))
    budget = Budget(max_usd=1.0)
    res = asyncio.run(prov.generate(_req(), budget=budget))
    k = m.kw[0]
    for bad in ("tool_choice", "tools", "temperature", "top_p", "top_k", "thinking"):
        assert bad not in k
    assert k["output_config"]["effort"] == "medium" and "format" not in k["output_config"]  # text is the default output
    assert "betas" not in k and "fallbacks" not in k  # study cells must not be silently served by another model
    assert k["system"][0]["text"] == "MATERIALS" and k["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert res.ok and res.text == "the answer" and res.served_model == "claude-sonnet-5-5-20261001"
    assert budget.ledger and budget.spent_usd > 0 and budget.reserved_usd == 0


def test_anthropic_model_fallback_is_opt_in():
    prov, m = _anthropic(_Resp(text="x"))
    prov._llm.allow_model_fallback = True
    asyncio.run(prov.generate(_req()))
    assert m.kw[0]["betas"] == [FALLBACK_BETA] and m.kw[0]["fallbacks"] == "default"


def test_anthropic_arm_a_sends_no_system_block():
    prov, m = _anthropic(_Resp())
    asyncio.run(prov.generate(_req(system="")))
    assert "system" not in m.kw[0]


def test_anthropic_refusal_is_checked_before_content_and_never_fabricated():
    prov, _ = _anthropic(_Resp(stop="refusal", guard=True))
    res = asyncio.run(prov.generate(_req()))
    assert res.refused and res.text is None and not res.ok and "refusal" in res.error


def test_anthropic_truncation_is_returned_flagged_not_retried():
    prov, m = _anthropic(_Resp(text="partial", stop="max_tokens"))
    res = asyncio.run(prov.generate(_req()))
    assert res.truncated and res.text == "partial" and len(m.kw) == 1


def test_anthropic_budget_exceeded_makes_no_api_call():
    from essay_grader.budget import BudgetExceeded

    prov, m = _anthropic(_Resp())
    with pytest.raises(BudgetExceeded):
        asyncio.run(prov.generate(_req(max_tokens=100000), budget=Budget(max_usd=0.0001)))
    assert m.kw == []


def test_openai_responses_body_and_parse():
    body = build_responses_body(_req(model="gpt-6-luna"))
    assert set(body) == {"model", "input", "max_output_tokens"} and body["model"] == "gpt-6-luna"
    assert body["input"][0]["content"][0]["text"] == "MATERIALS\n\nPROMPT"
    assert build_responses_body(_req(system=""))["input"][0]["content"][0]["text"] == "PROMPT"
    calls = []

    async def transport(b):
        calls.append(b)
        return {"model": "gpt-6-luna-2026", "status": "completed", "output": [
            {"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": "A"}, {"type": "output_text", "text": "B"}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 400}}}

    budget = Budget(max_usd=1.0)
    res = asyncio.run(OpenAIResponsesProvider(transport).generate(_req(model="gpt-6-luna"), budget=budget))
    assert len(calls) == 1 and res.text == "AB" and res.served_model == "gpt-6-luna-2026"
    assert res.usage.input_tokens == 600 and res.usage.cache_read_tokens == 400 and res.usage.output_tokens == 50
    assert budget.ledger and budget.reserved_usd == 0


def test_openai_refusal_and_incomplete():
    r = parse_responses({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}, "m")
    assert r.refused and r.text is None
    t = parse_responses({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                         "output": [{"type": "message", "content": [{"type": "output_text", "text": "cut"}]}]}, "m")
    assert t.truncated and t.text == "cut"
    with pytest.raises(ValueError):
        parse_responses({"status": "failed", "error": {"message": "x"}, "output": []}, "m")


def test_google_is_a_stub_and_providers_are_pluggable():
    with pytest.raises(NotImplementedError):
        asyncio.run(GoogleProvider().generate(_req()))
    from essay_grader.study import providers

    class Third:
        name = "third"

        async def generate(self, req, *, budget=None):
            return await FakeProvider("third").generate(req, budget=budget)

    providers.register_provider("third", lambda dry: Third())
    try:
        assert make_provider("third").name == "third"
    finally:
        providers.PROVIDER_FACTORIES.pop("third")


def test_dry_run_factories_return_offline_fakes():
    for name in ("anthropic", "openai", "google"):
        assert isinstance(make_provider(name, dry_run=True), FakeProvider)
