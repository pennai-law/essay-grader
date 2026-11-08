import asyncio
import json

import pytest

from essay_grader.budget import Budget, BudgetExceeded
from essay_grader.fake import FakeLLM, fake_decisions_transport
from essay_grader.graders.claude_grader import ClaudeGrader
from essay_grader.graders.openai_decisions_grader import OpenAIDecisionsGrader
from essay_grader.ingest import load_answers
from essay_grader.llm import DEFAULT_CLAUDE_MODEL, SONNET_MODEL
from essay_grader.pipeline import GradeConfig, _estimate_pending, run_pipeline

OPUS = DEFAULT_CLAUDE_MODEL


class FatFake(FakeLLM):
    """Reports 4000 output tokens per call (a verbose model), so real spend outruns the typical-output estimate."""

    async def _create(self, request, hint):
        resp = await super()._create(request, hint)
        if resp.stop_reason != "refusal":
            resp.usage.output_tokens = 4000
        return resp


def gs(llm):
    return [ClaudeGrader(llm, retry_delay=0), OpenAIDecisionsGrader(fake_decisions_transport, retry_delay=0)]


def go(locked, fix, exam_text, tmp_path, graders, budget, run_id="r1", **kw):
    cfg = GradeConfig(run_id=run_id, out_dir=tmp_path, seed=3, **kw)
    return asyncio.run(run_pipeline(locked, exam_text, load_answers(fix / "answers", locked), graders, cfg, budget=budget))


def estimates(locked, fix, exam_text, model):
    pending = list(enumerate(load_answers(fix / "answers", locked)))
    return _estimate_pending(gs(FakeLLM(model)), locked, exam_text, pending)


def test_no_budget_means_no_gating(locked, fix, exam_text, tmp_path):
    d = go(locked, fix, exam_text, tmp_path, gs(FakeLLM(OPUS)), None)
    meta = json.loads((d / "run_metadata.json").read_text())
    assert meta["status"] == "complete" and meta["degradations"] == [] and meta["budget"]["max_usd"] is None


def test_auto_downgrade_opus_to_sonnet_when_estimate_exceeds_budget(locked, fix, exam_text, tmp_path):
    e_opus, e_son = estimates(locked, fix, exam_text, OPUS), estimates(locked, fix, exam_text, SONNET_MODEL)
    assert e_son < e_opus
    llm = FakeLLM(OPUS)
    budget = Budget(max_usd=(e_opus + e_son) / 2)
    d = go(locked, fix, exam_text, tmp_path, gs(llm), budget)
    meta = json.loads((d / "run_metadata.json").read_text())
    assert meta["status"] == "complete"
    dg = meta["degradations"]
    assert len(dg) == 1 and dg[0]["kind"] == "grader_model_downgrade" and dg[0]["from"] == OPUS and dg[0]["to"] == SONNET_MODEL
    assert dg[0]["estimate_before_usd"] > dg[0]["remaining_usd"] >= dg[0]["estimate_after_usd"]
    claude = next(g for g in meta["graders"] if g["name"] == "claude")
    assert claude["model"] == SONNET_MODEL and claude["requested_model"] == OPUS
    assert {r["model"] for r in llm.requests} == {SONNET_MODEL}
    assert meta["budget"]["spent_usd"] <= budget.max_usd


def test_no_downgrade_when_it_fits(locked, fix, exam_text, tmp_path):
    llm = FakeLLM(OPUS)
    d = go(locked, fix, exam_text, tmp_path, gs(llm), Budget(max_usd=100))
    assert json.loads((d / "run_metadata.json").read_text())["degradations"] == []
    assert {r["model"] for r in llm.requests} == {OPUS}


def test_budget_exceeded_when_even_sonnet_does_not_fit(locked, fix, exam_text, tmp_path):
    e_son = estimates(locked, fix, exam_text, SONNET_MODEL)
    llm = FakeLLM(OPUS)
    with pytest.raises(BudgetExceeded) as ei:
        go(locked, fix, exam_text, tmp_path, gs(llm), Budget(max_usd=e_son / 2))
    assert ei.value.estimate_usd == pytest.approx(e_son) and ei.value.remaining_usd == pytest.approx(e_son / 2)
    assert "exceeds the remaining budget" in str(ei.value) and "downgrading" in str(ei.value)
    assert llm.requests == [] and not (tmp_path / "r1").exists()  # nothing was called or written


def test_mid_run_stop_is_clean_and_resumable(locked, fix, exam_text, tmp_path):
    e_son = estimates(locked, fix, exam_text, SONNET_MODEL)
    llm = FatFake(SONNET_MODEL)
    budget = Budget(max_usd=e_son)  # passes the pre-run estimate, but real spend is higher
    d = go(locked, fix, exam_text, tmp_path, gs(llm), budget, concurrency=1)
    meta = json.loads((d / "run_metadata.json").read_text())
    lines1 = (d / "results.jsonl").read_text().splitlines()
    assert meta["status"] == "stopped_budget_exhausted" and "Re-run with the same --run-id" in meta["resume"]
    assert 1 <= len(lines1) < 3 and meta["answers_remaining"] == 3 - len(lines1)
    assert budget.spent_usd <= budget.max_usd  # never overspent
    assert all(json.loads(l)["status"] in {"agreed", "needs_human_review"} for l in lines1)  # finished records are whole
    for f in ("scores.csv", "review_queue.csv", "agreement_report.md"):
        assert (d / f).exists()
    # resume later with a fresh budget: finished answers are kept untouched and skipped
    d = go(locked, fix, exam_text, tmp_path, gs(FakeLLM(SONNET_MODEL)), Budget(max_usd=10))
    lines2 = (d / "results.jsonl").read_text().splitlines()
    assert len(lines2) == 3 and lines2[:len(lines1)] == lines1
    meta = json.loads((d / "run_metadata.json").read_text())
    assert meta["status"] == "complete" and meta["answers_remaining"] == 0


def test_resume_keeps_the_downgraded_model(locked, fix, exam_text, tmp_path):
    e_opus, e_son = estimates(locked, fix, exam_text, OPUS), estimates(locked, fix, exam_text, SONNET_MODEL)
    go(locked, fix, exam_text, tmp_path, gs(FakeLLM(OPUS)), Budget(max_usd=(e_opus + e_son) / 2), concurrency=1)
    # nothing pending now, but force a re-grade of one by deleting its record, with a budget that would allow Opus
    path = tmp_path / "r1" / "results.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")
    llm = FakeLLM(OPUS)
    d = go(locked, fix, exam_text, tmp_path, gs(llm), Budget(max_usd=100))
    assert {r["model"] for r in llm.requests} == {SONNET_MODEL}  # one run, one Claude model
    meta = json.loads((d / "run_metadata.json").read_text())
    assert len(meta["degradations"]) == 1 and next(g for g in meta["graders"] if g["name"] == "claude")["requested_model"] == OPUS


def test_refusal_in_pipeline_goes_to_human_review_not_a_grade(locked, fix, exam_text, tmp_path):
    d = go(locked, fix, exam_text, tmp_path, gs(FakeLLM(SONNET_MODEL, refuse_all=True)), Budget(max_usd=10))
    recs = [json.loads(l) for l in (d / "results.jsonl").read_text().splitlines()]
    assert len(recs) == 3 and all(r["status"] == "needs_human_review" and r["suggested_total"] is None for r in recs)
    assert all(any(f.startswith("grader_error:claude") for f in r["flags"]) for r in recs)
