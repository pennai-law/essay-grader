import asyncio
from pathlib import Path

import pytest

from essay_grader import questionnaire as qm
from essay_grader.budget import Budget, BudgetExceeded
from essay_grader.calibrate import calibrate
from essay_grader.fake import FakeLLM, fake_decisions_transport
from essay_grader.graders.openai_decisions_grader import OpenAIDecisionsGrader
from essay_grader.harden import (ARCHETYPE_PRIORITY, HardenConfig, harden, plan, render_markdown, write_report)
from essay_grader.ingest import Answer
from essay_grader.llm import DEFAULT_CLAUDE_MODEL, SONNET_MODEL

OPUS = DEFAULT_CLAUDE_MODEL


def run_harden(draft, exam_text, llm=None, budget=None, cfg=None, decisions=True):
    dec = OpenAIDecisionsGrader(fake_decisions_transport, retry_delay=0) if decisions else None
    return asyncio.run(harden(draft, exam_text, "syllabus", llm or FakeLLM(), decisions=dec, budget=budget,
                              cfg=cfg or HardenConfig(use_decisions=decisions, concurrency=3)))


# ---- planner -------------------------------------------------------------------------------------------------------

def test_plan_defaults_when_budget_allows(draft, exam_text):
    p = plan(exam_text, "syl", draft, Budget(100))
    assert p.feasible and not p.degradations
    assert p.critique_model == OPUS and p.synth_model == SONNET_MODEL and p.stress_model == SONNET_MODEL
    assert p.questionnaire_model == OPUS
    assert p.archetypes == ARCHETYPE_PRIORITY and len(p.archetypes) == 11
    assert p.claude_grades_per_question == 11 and p.k == 5
    assert set(p.breakdown) == {"critique", "synthetic_answers", "stress_claude", "stress_decisions", "questionnaire"}


def test_plan_scales_to_the_ask(draft, exam_text):
    big = draft.model_copy(deep=True)
    big.questions.append(big.questions[0].model_copy(deep=True, update={"id": "Q2"}))
    p1, p2 = plan(exam_text, "s", draft, Budget(100)), plan(exam_text, "s", big, Budget(100))
    assert len(p2.archetypes) == 8 < len(p1.archetypes)  # multi-question exam: fewer archetypes each
    assert p2.estimate_usd > p1.estimate_usd
    long_exam = plan(exam_text * 200, "s", draft, Budget(100))
    assert long_exam.estimate_usd > p1.estimate_usd and len(long_exam.archetypes) == 8


def test_plan_degradation_order(draft, exam_text):
    full = plan(exam_text, "syl", draft, Budget(100)).estimate_usd
    seen = []
    for frac in (0.95, 0.7, 0.55, 0.45, 0.35, 0.28):  # progressively tighter budgets
        p = plan(exam_text, "syl", draft, Budget(full * frac))
        seen.append(p)
    # 1) fewer Claude stress grades first, archetypes untouched
    assert seen[0].claude_grades_per_question < 11 and len(seen[0].archetypes) == 11 and seen[0].critique_model == OPUS
    # 2) then fewer archetypes, then 3) Sonnet for the critique
    tight = [p for p in seen if len(p.archetypes) < 11]
    assert tight and all(p.claude_grades_per_question <= 3 for p in tight)
    first_sonnet_critique = next(i for i, p in enumerate(seen) if p.critique_model == SONNET_MODEL)
    first_fewer_arch = next(i for i, p in enumerate(seen) if len(p.archetypes) < 11)
    first_fewer_claude = next(i for i, p in enumerate(seen) if p.claude_grades_per_question < 11)
    assert first_fewer_claude <= first_fewer_arch <= first_sonnet_critique
    for p in seen:
        if p.critique_model == SONNET_MODEL:
            assert len(p.archetypes) == 4  # archetypes were exhausted before the critique was downgraded
        order = [d.split(":")[0].split(" on ")[0] for d in p.degradations]
        assert order == sorted(order, key=lambda d: ["fewer Claude stress grades", "fewer archetypes", "critique", "questionnaire", "no Claude stress grades (Decisions repeats only; no evidence quotes)"].index(d)), order
    assert all(p.feasible for p in seen) and all(p.estimate_usd <= full * f + 1e-9 for p, f in zip(seen, (0.95, 0.7, 0.55, 0.45, 0.35, 0.28)))


def test_plan_infeasible_is_flagged_and_harden_raises(draft, exam_text):
    assert not plan(exam_text, "syl", draft, Budget(0.01)).feasible
    llm = FakeLLM()
    with pytest.raises(BudgetExceeded) as ei:
        run_harden(draft, exam_text, llm, Budget(0.01))
    assert ei.value.estimate_usd > 0.01 and llm.calls == 0


# ---- end to end ----------------------------------------------------------------------------------------------------

def test_harden_offline_one_questionnaire(draft, exam_text, tmp_path):
    llm = FakeLLM()
    budget = Budget(max_usd=5.0)
    rep = run_harden(draft, exam_text, llm, budget)
    archs = {a["archetype"] for a in rep["synthetic_answers"]}
    assert len(archs) == 11
    # exactly one critique, one synthetic call, one questionnaire call; 11 Claude stress grades
    kinds = [h for h in (r["output_config"]["effort"] for r in llm.requests)]
    assert len(llm.requests) == 1 + 1 + 11 + 1 and kinds.count("medium") == 1
    purposes = {e.purpose for e in budget.ledger}
    assert {"harden-critique", "harden-synthetic", "harden-stress-claude", "harden-questionnaire", "grade-decisions"} <= purposes
    assert sum(e.purpose == "grade-decisions" for e in budget.ledger) == 11 * 5
    assert budget.spent_usd == pytest.approx(rep["budget"]["spent_usd"]) and budget.spent_usd < 5
    qn = qm.Questionnaire.model_validate(rep["questionnaire"])
    assert 0 < len(qn.questions) <= 12
    assert [q.id for q in qn.questions] == [f"H{i}" for i in range(1, len(qn.questions) + 1)]
    imps = [q.impact for q in qn.questions]
    assert imps == sorted(imps, reverse=True)
    for q in qn.questions:  # each carries a verbatim-from-synthetic excerpt
        assert q.excerpt and q.answer_id
    assert all("claude_evidence" in c for r in rep["stress"] for c in r["criteria"])
    # one call produced the projections: they are clamped to each criterion's range and anchored to the measured points
    maxes = {c.id: c.max_points for q in draft.questions for c in q.criteria}
    measured = {r["answer_id"]: {c["criterion_id"]: sum(c["points"]) / len(c["points"]) for c in r["criteria"]} for r in rep["stress"]}
    projected = [(q, o, p) for q in qn.questions for o in q.options for p in (o.projected or [])]
    assert projected, "the fake questionnaire carries projections"
    for q, o, p in projected:
        assert 0 <= p.after_points <= maxes[p.criterion_id] and 0 <= p.before_points <= maxes[p.criterion_id]
        assert p.before_points == pytest.approx(round(measured[q.answer_id][p.criterion_id], 2))
    assert any(p.after_points == 0 for _, _, p in projected) and any(p.after_points == maxes[p.criterion_id] for _, _, p in projected)
    from essay_grader.prompts import QUESTIONNAIRE_SYSTEM
    assert "before_points" in QUESTIONNAIRE_SYSTEM and "after_points" in QUESTIONNAIRE_SYSTEM  # asked for in the same call
    d = write_report(rep, tmp_path)
    for f in ("questionnaire.json", "questionnaire.md", "harden_report.md", "harden_report.json"):
        assert (d / f).exists()
    md = render_markdown(rep)
    assert "## Plan" in md and "Archetype outcomes" in md


def test_harden_degrades_to_tight_budget_and_records_it(draft, exam_text):
    full = plan(exam_text, "syl", draft, Budget(100)).estimate_usd
    budget = Budget(max_usd=full * 0.5)
    rep = run_harden(draft, exam_text, FakeLLM(), budget)
    assert rep["degradations"] and rep["plan"]["degradations"] == rep["degradations"][:len(rep["plan"]["degradations"])]
    assert len(rep["plan"]["archetypes"]) < 11
    assert budget.spent_usd <= budget.max_usd


def test_harden_without_decisions_and_with_refusals(draft, exam_text):
    rep = run_harden(draft, exam_text, FakeLLM(refuse_kinds={"critique"}), decisions=False)
    assert rep["critique_findings"] == [] and any(e.startswith("critique: refusal") for e in rep["errors"])
    assert rep["questionnaire"]["questions"]  # the questionnaire still gets written
    # questionnaire call refused -> mechanical fallback from critique findings, flagged
    rep = run_harden(draft, exam_text, FakeLLM(refuse_kinds={"questionnaire"}), decisions=False)
    qn = qm.Questionnaire.model_validate(rep["questionnaire"])
    assert qn.questions and any("without an LLM call" in d for d in rep["degradations"])
    assert any("unavailable" in w for w in qn.warnings)
    # everything refused: no crash, an empty (but valid) questionnaire, no fabricated stress results
    rep = run_harden(draft, exam_text, FakeLLM(refuse_all=True), decisions=False)
    assert rep["stress"] == [] and rep["synthetic_answers"] == [] and rep["questionnaire"]["questions"] == []


# ---- calibration uses the same format ----------------------------------------------------------------------------

def test_calibrate_emits_questionnaire_and_takes_budget(locked, exam_text):
    samples = [Answer("S001", "Q1", "text one", "S001.txt"), Answer("S002", "Q1", "text two", "S002.txt")]
    budget = Budget(max_usd=1.0)
    new, rep = asyncio.run(calibrate(locked, exam_text, samples, FakeLLM(), budget=budget))
    assert new.version == 2 and not new.locked and new.calibration.sample_ids == ["S001", "S002"]
    assert new.calibration.parent_hash == locked.content_hash
    assert not any(c.edge_case_rulings for c in new.questions[0].criteria)  # nothing applied until the professor answers
    qn = qm.Questionnaire.model_validate(rep["questionnaire"])
    assert qn.run_kind == "calibration" and [q.id for q in qn.questions] == ["C1"] and qn.questions[0].excerpt == ""
    assert budget.ledger and budget.ledger[0].purpose == "calibrate"
    final = qm.apply_answers(new, qn, {})
    assert final.version == 3 and any(r.startswith("[fake ruling]") for c in final.questions[0].criteria for r in c.edge_case_rulings)
    assert final.provenance[0].run_kind == "calibration"
    final.lock()
    # over budget: skipped cleanly, recorded
    new2, rep2 = asyncio.run(calibrate(locked, exam_text, samples, FakeLLM(), budget=Budget(max_usd=1e-9)))
    assert rep2["questionnaire"]["questions"] == [] and rep2["degradations"]
