"""Optional calibration: find approaches in a small real sample that the locked rubric does not cover.

Output is a NEW rubric version (unlocked) that must be reviewed and re-locked before grading.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .ingest import Answer
from .llm import StructuredLLM, UsageTracker
from .prompts import CALIBRATE_SYSTEM, prompt_hashes, render_question
from .rubric import Calibration, Rubric, save_rubric


class OpenQuestion(BaseModel):
    description: str
    sample_ids: list[str] = Field(description="Ids of sample answers that exhibit this.")
    criterion_id: str
    proposed_ruling: str = Field(description="General ruling; must not quote or describe a specific student.")


class CalibrateOutput(BaseModel):
    open_questions: list[OpenQuestion]


async def calibrate(rubric: Rubric, exam_text: str, samples: list[Answer], llm: StructuredLLM,
                    tracker: UsageTracker | None = None) -> tuple[Rubric, dict]:
    rubric.require_locked()
    tracker = tracker or UsageTracker()
    valid_ids = {a.student_id for a in samples}
    used: set[str] = set()
    accepted: list[tuple[str, str]] = []
    report_items: list[dict] = []
    for q in rubric.questions:
        qs = [a for a in samples if a.question_id == q.id]
        if not qs:
            continue
        used |= {a.student_id for a in qs}
        body = "\n\n".join(f'<sample_answer id="{a.student_id}">\n{a.text}\n</sample_answer>' for a in qs)
        res = await llm.call(
            prefix=CALIBRATE_SYSTEM.format(exam=exam_text, question_id=q.id, rubric=render_question(rubric, q)),
            user=body + "\n\nList open questions with proposed rulings.",
            tool_name="report_open_questions", tool_description="Report rubric open questions and proposed rulings.",
            schema=CalibrateOutput, max_tokens=6000,
            hint={"kind": "calibrate", "question": q.model_dump(), "sample_ids": [a.student_id for a in qs]})
        tracker.add("calibrate", llm.model, res.usage)
        crit_ids = {c.id for c in q.criteria}
        for oq in res.parsed.open_questions:  # type: ignore[attr-defined]
            item = {"question_id": q.id, **oq.model_dump(), "applied": False, "warnings": []}
            bad_ids = [s for s in oq.sample_ids if s not in valid_ids]
            if bad_ids:
                item["warnings"].append(f"unknown sample ids dropped: {bad_ids}")
                item["sample_ids"] = [s for s in oq.sample_ids if s in valid_ids]
            if oq.criterion_id in crit_ids:
                accepted.append((oq.criterion_id, oq.proposed_ruling))
                item["applied"] = True
            else:
                item["warnings"].append(f"criterion {oq.criterion_id!r} not in question {q.id}; ruling not applied")
            report_items.append(item)
    new = rubric.with_rulings(accepted, calibration=Calibration(
        sample_ids=sorted(used), parent_version=rubric.version, parent_hash=rubric.content_hash, model=llm.model))
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "exam_id": rubric.exam_id, "from_version": rubric.version, "to_version": new.version,
              "parent_hash": rubric.content_hash, "model": llm.model, "prompt_hashes": prompt_hashes(),
              "calibration_sample_ids": sorted(used), "open_questions": report_items, "usage": tracker.summary(),
              "next_step": "Review rulings in the new draft rubric, then `essay-grader lock` it before grading."}
    return new, report


def write_calibration(new: Rubric, report: dict, source: Path, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = source.name.split(".")[0]
    rpath = save_rubric(new, out_dir / f"{stem}.v{new.version}.yaml")
    cpath = out_dir / f"calibration_report.v{new.version}.json"
    cpath.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return rpath, cpath
