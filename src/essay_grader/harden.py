"""Rubric hardening: critique -> adversarial synthetic answers -> repeated stress grading -> report.

Runs BEFORE any student exam is seen; uses only the exam question, syllabus and the draft rubric.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import pvariance
from typing import Literal

from pydantic import BaseModel, Field

from .graders.claude_grader import ClaudeGrader
from .ingest import apply_length_policy
from .llm import StructuredLLM, UsageTracker, Usage
from .prompts import (ARCHETYPES, CRITIQUE_SYSTEM, STRESS_SYSTEM, SYNTH_SYSTEM, SYNTH_USER, prompt_hashes,
                      render_rubric_all)
from .rubric import Rubric

# Expected outcome band (fraction of the question's max points) per archetype; None = no expectation.
EXPECTED_BAND: dict[str, tuple[float, float] | None] = {
    "strong": (0.75, 1.0), "median": (0.35, 0.8), "weak": (0.0, 0.4), "overlong": None,
    "rabbit_hole": (0.0, 0.5), "wrong_reasoning": (0.0, 0.6), "contrary_conclusion": (0.6, 1.0),
    "laundry_list": (0.0, 0.5), "wrong_jurisdiction": (0.0, 0.4), "terse_correct": (0.6, 1.0),
    "outside_syllabus": (0.0, 0.5),
}


class Amendment(BaseModel):
    type: Literal["edge_case_ruling", "reword_descriptor", "add_policy", "add_criterion"]
    criterion_id: str | None = None
    text: str


class CritiqueFinding(BaseModel):
    category: Literal["ambiguous", "not_observable", "double_counting", "missing_issue",
                      "style_over_substance", "missing_policy", "other"]
    criterion_id: str | None = None
    issue: str
    evidence: str = Field(description="Quote or cite the exam/syllabus/rubric text that supports this.")
    amendment: Amendment


class CritiqueOutput(BaseModel):
    findings: list[CritiqueFinding]


class SynthOutput(BaseModel):
    answer: str


class StressAmendment(BaseModel):
    flag_id: str
    explanation: str
    amendment: Amendment


class StressOutput(BaseModel):
    findings: list[StressAmendment]


@dataclass
class HardenConfig:
    n_synthetic: int = 11  # per question; cycles through archetypes
    k: int = 3  # repeats per synthetic answer
    expected_words: int = 800
    spread_threshold: float = 1.0  # points
    concurrency: int = 4
    retry_delay: float = 1.0


async def _gather(coros, limit: int):
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros))


async def harden(rubric: Rubric, exam_text: str, syllabus_text: str, llm: StructuredLLM,
                 cfg: HardenConfig | None = None, tracker: UsageTracker | None = None) -> dict:
    cfg = cfg or HardenConfig()
    tracker = tracker or UsageTracker()
    grader = ClaudeGrader(llm, allow_draft=True, retry_delay=cfg.retry_delay)  # stress-grading a draft is the point
    expected = rubric.policies.length_policy.word_cap or cfg.expected_words

    # (a) critique
    res = await llm.call(prefix=CRITIQUE_SYSTEM.format(exam=exam_text, syllabus=syllabus_text,
                                                       rubric_all=render_rubric_all(rubric)),
                         user="Review the draft rubric and report all findings.",
                         tool_name="report_findings", tool_description="Report rubric critique findings.",
                         schema=CritiqueOutput, max_tokens=8000,
                         hint={"kind": "critique", "rubric": rubric.model_dump()})
    tracker.add("harden-critique", llm.model, res.usage)
    critique: CritiqueOutput = res.parsed  # type: ignore[assignment]

    # (b) synthetic answers
    names = list(ARCHETYPES)
    specs = [(q.id, names[i % len(names)], i // len(names) + 1, i)
             for q in rubric.questions for i in range(cfg.n_synthetic)]

    async def synth(qid: str, arch: str, variant: int, idx: int) -> dict:
        words = expected * 5 // 2 if arch == "overlong" else (expected // 4 if arch == "terse_correct" else expected)
        r = await llm.call(
            prefix=SYNTH_SYSTEM.format(exam=exam_text, syllabus=syllabus_text),
            user=SYNTH_USER.format(question_id=qid, archetype=arch, archetype_desc=ARCHETYPES[arch],
                                   words=words, variant=variant),
            tool_name="submit_answer", tool_description="Submit the synthetic answer text.",
            schema=SynthOutput, max_tokens=8000, hint={"kind": "synth", "archetype": arch, "variant": variant, "words": words})
        tracker.add("harden-synth", llm.model, r.usage)
        return {"id": f"SYN{idx + 1:03d}", "question_id": qid, "archetype": arch,
                "text": r.parsed.answer}  # type: ignore[attr-defined]

    answers = await _gather([synth(*s) for s in specs], cfg.concurrency)

    # (c) stress grade K times each
    async def grade_once(a: dict, rep: int) -> dict:
        text, info = apply_length_policy(a["text"], rubric.policies.length_policy)
        r = await grader.grade(rubric, rubric.question(a["question_id"]), exam_text, text)
        tracker.add("harden-stress", llm.model, Usage.from_dict(r.usage))
        return {"answer_id": a["id"], "rep": rep, "deduction": info["deduction"],
                "points": {g.criterion_id: g.points for g in r.grades},
                "labels": {g.criterion_id: g.label for g in r.grades}}

    runs = await _gather([grade_once(a, k) for a in answers for k in range(cfg.k)], cfg.concurrency)

    flags: list[dict] = []
    stress_rows: list[dict] = []
    for a in answers:
        q = rubric.question(a["question_id"])
        mine = [r for r in runs if r["answer_id"] == a["id"]]
        totals = [sum(r["points"].values()) - r["deduction"] for r in mine]
        frac = (sum(totals) / len(totals)) / q.max_points
        band = EXPECTED_BAND.get(a["archetype"])
        row = {"answer_id": a["id"], "question_id": q.id, "archetype": a["archetype"],
               "words": len(a["text"].split()), "mean_fraction": round(frac, 3),
               "total_spread": round(max(totals) - min(totals), 3), "expected_band": band, "criteria": []}
        if band and not (band[0] - 1e-9 <= frac <= band[1] + 1e-9):
            flags.append({"kind": "archetype_outcome", "answer_id": a["id"], "question_id": q.id,
                          "archetype": a["archetype"], "mean_fraction": row["mean_fraction"], "expected_band": band,
                          "issue": f"{a['archetype']} answer {a['id']} scored {frac:.0%} of {q.max_points:g} pts; expected {band[0]:.0%}-{band[1]:.0%}."})
        for c in q.criteria:
            pts = [r["points"][c.id] for r in mine]
            labs = {r["labels"][c.id] for r in mine}
            spread = max(pts) - min(pts)
            unstable = spread > cfg.spread_threshold + 1e-9 or (c.kind != "score" and len(labs) > 1)
            row["criteria"].append({"criterion_id": c.id, "points": pts, "spread": spread,
                                    "variance": round(pvariance(pts), 4), "unstable": unstable})
            if unstable:
                flags.append({"kind": "unstable", "answer_id": a["id"], "question_id": q.id, "criterion_id": c.id,
                              "archetype": a["archetype"], "points": pts, "spread": spread,
                              "issue": f"Criterion {c.id} scored {pts} across {len(pts)} repeats of {a['archetype']} answer {a['id']}."})
        stress_rows.append(row)
    for i, f in enumerate(flags, 1):
        f["flag_id"] = f"S{i}"

    # (d) amendments for stress flags
    stress_amend: dict[str, StressAmendment] = {}
    if flags:
        r = await llm.call(
            prefix=STRESS_SYSTEM.format(rubric_all=render_rubric_all(rubric)),
            user="Flagged items:\n" + json.dumps(flags, indent=1) + "\n\nPropose one amendment per flag_id.",
            tool_name="propose_amendments", tool_description="Propose rubric amendments for flagged items.",
            schema=StressOutput, max_tokens=8000, hint={"kind": "stress", "flags": flags})
        tracker.add("harden-amend", llm.model, r.usage)
        stress_amend = {s.flag_id: s for s in r.parsed.findings}  # type: ignore[attr-defined]

    findings = []
    for i, f in enumerate(critique.findings, 1):
        findings.append({"id": f"C{i}", "source": "critique", "category": f.category, "criterion_id": f.criterion_id,
                         "issue": f.issue, "evidence": f.evidence, "amendment": f.amendment.model_dump()})
    for f in flags:
        s = stress_amend.get(f["flag_id"])
        findings.append({"id": f["flag_id"], "source": "stress", "category": f["kind"],
                         "criterion_id": f.get("criterion_id"), "issue": f["issue"],
                         "evidence": {k: v for k, v in f.items() if k not in ("issue",)},
                         "explanation": s.explanation if s else None,
                         "amendment": s.amendment.model_dump() if s else None})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exam_id": rubric.exam_id, "rubric_version": rubric.version, "draft_rubric_hash": rubric.compute_hash(),
        "model": llm.model, "config": cfg.__dict__, "prompt_hashes": prompt_hashes(),
        "findings": findings, "stress": stress_rows, "synthetic_answers": answers,
        "usage": tracker.summary(),
        "note": "Synthetic answers are fictional. Professor reviews/edits findings, then runs `essay-grader lock`.",
    }


def render_markdown(rep: dict) -> str:
    L = [f"# Rubric hardening report: {rep['exam_id']} v{rep['rubric_version']}", "",
         f"- Draft rubric hash: `{rep['draft_rubric_hash'][:16]}`  - Model: `{rep['model']}`  - Generated: {rep['generated_at']}",
         "- Review each finding; edit the draft rubric to accept amendments; then `essay-grader lock`.", ""]
    L += ["## Archetype outcomes (mean over repeats)", "",
          "| Answer | Question | Archetype | Words | Mean % of max | Expected | Total spread |", "|---|---|---|---|---|---|---|"]
    for r in rep["stress"]:
        b = r["expected_band"]
        exp = f"{b[0]:.0%}-{b[1]:.0%}" if b else "n/a"
        bad = " **(!)**" if b and not (b[0] <= r["mean_fraction"] <= b[1]) else ""
        L.append(f"| {r['answer_id']} | {r['question_id']} | {r['archetype']} | {r['words']} | {r['mean_fraction']:.0%}{bad} | {exp} | {r['total_spread']} |")
    L += ["", "## Findings", ""]
    if not rep["findings"]:
        L.append("None.")
    for f in rep["findings"]:
        a = f["amendment"]
        L += [f"### {f['id']} [{f['source']}/{f['category']}] criterion: {f['criterion_id'] or '-'}", "",
              f"**Issue:** {f['issue']}", "",
              f"**Evidence:** {f['evidence'] if isinstance(f['evidence'], str) else '`' + json.dumps(f['evidence']) + '`'}", ""]
        if f.get("explanation"):
            L += [f"**Why:** {f['explanation']}", ""]
        L += [f"**Proposed amendment ({a['type']}, criterion {a['criterion_id'] or '-'}):** {a['text']}" if a
              else "**Proposed amendment:** none returned", ""]
    L += ["## Usage", ""] + [f"- {u['role']} / {u['model']}: in {u['input_tokens']}, out {u['output_tokens']}, cache-read {u['cache_read_tokens']}, est. ${u['estimated_cost_usd']}" for u in rep["usage"]]
    return "\n".join(L) + "\n"


def write_report(rep: dict, out_dir: str | Path) -> Path:
    d = Path(out_dir) / f"harden-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "harden_report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (d / "harden_report.md").write_text(render_markdown(rep), encoding="utf-8")
    return d
