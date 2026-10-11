"""Optional calibration: find approaches in a small real sample that the locked rubric does not cover.

Output is a NEW unlocked draft (calibration metadata only, no rulings yet) plus a questionnaire in the SAME format as
hardening, so one form serves both. The professor answers it once; `questionnaire.apply_answers` writes the rulings;
then re-lock before grading.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import questionnaire as qmod
from .budget import Budget, BudgetExceeded
from .ingest import Answer
from .llm import StructuredLLM
from .prompts import CALIBRATE_SYSTEM, prompt_hashes, render_question
from .rubric import Calibration, Rubric, save_rubric

CALIBRATE_MAX_TOKENS = 6000


class CalibrateOutput(qmod.QuestionnaireOutput):
    """Same shape as the hardening questionnaire output (a list of RawQuestion)."""


async def calibrate(rubric: Rubric, exam_text: str, samples: list[Answer], llm: StructuredLLM, *,
                    budget: Budget | None = None, max_questions: int = 12,
                    run_id: str | None = None) -> tuple[Rubric, dict]:
    """-> (new unlocked draft, report). `report["questionnaire"]` is a `Questionnaire` dict (ids C1, C2, ...).

    Stops cleanly when the budget runs out: remaining questions are skipped and recorded in `report["degradations"]`.
    """
    rubric.require_locked()
    budget = budget if budget is not None else Budget.unlimited()
    run_id = run_id or "calibration-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    used: set[str] = set()
    raw: list[qmod.RawQuestion] = []
    degradations: list[str] = []
    errors: list[str] = []
    for q in rubric.questions:
        qs = [a for a in samples if a.question_id == q.id]
        if not qs:
            continue
        used |= {a.student_id for a in qs}
        body = "\n\n".join(f'<sample_answer id="{a.student_id}">\n{a.text}\n</sample_answer>' for a in qs)
        try:
            res = await llm.call(
                prefix=CALIBRATE_SYSTEM.format(exam=exam_text, question_id=q.id, rubric=render_question(rubric, q),
                                               max_questions=max_questions),
                user=body + "\n\nList the open questions for the professor.", schema=CalibrateOutput,
                max_tokens=CALIBRATE_MAX_TOKENS, effort="high", purpose="calibrate", budget=budget,
                hint={"kind": "calibrate", "criterion_ids": [c.id for c in q.criteria], "sample_ids": sorted(a.student_id for a in qs)})
        except BudgetExceeded as e:
            degradations.append(f"question {q.id} skipped: {e}")
            continue
        if not res.ok:
            errors.append(f"question {q.id}: {res.error}")
            continue
        crit_ids = {c.id for c in q.criteria}
        for rq in res.parsed.questions:  # type: ignore[attr-defined]
            if rq.criterion_id is not None and rq.criterion_id not in crit_ids:
                degradations.append(f"dropped calibration question on criterion {rq.criterion_id!r} (not in {q.id})")
                continue
            # never keep anything resembling student text: excerpts are not used for calibration
            raw.append(rq.model_copy(update={"excerpt": "", "answer_id": None}))
    draft = rubric.with_rulings([], calibration=Calibration(
        sample_ids=sorted(used), parent_version=rubric.version, parent_hash=rubric.content_hash, model=llm.model))
    qn = qmod.build_questionnaire(draft, raw, run_id=run_id, run_kind="calibration", max_questions=max_questions,
                                  model=llm.model, id_prefix="C", warnings=degradations + errors)
    report = {"run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "exam_id": rubric.exam_id, "from_version": rubric.version, "to_version": draft.version,
              "parent_hash": rubric.content_hash, "model": llm.model, "prompt_hashes": prompt_hashes(),
              "calibration_sample_ids": sorted(used), "questionnaire": qn.model_dump(),
              "degradations": degradations, "errors": errors, "budget": budget.summary(),
              "next_step": "Answer the questionnaire once, `essay-grader apply-answers`, then `essay-grader lock` before grading."}
    return draft, report


def write_calibration(new: Rubric, report: dict, source: Path, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = source.name.split(".")[0]
    rpath = save_rubric(new, out_dir / f"{stem}.v{new.version}.yaml")
    cpath = out_dir / f"calibration_report.v{new.version}.json"
    cpath.write_text(json.dumps(report, indent=2), encoding="utf-8")
    qn = qmod.Questionnaire.model_validate(report["questionnaire"])
    (out_dir / "questionnaire.json").write_text(qn.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "questionnaire.md").write_text(qmod.render_markdown(qn), encoding="utf-8")
    return rpath, cpath
