"""Budgeted, one-shot rubric hardening: plan -> critique + synthetic answers -> stress grading -> ONE questionnaire.

Runs BEFORE any student exam is seen; uses only the exam question, syllabus and the draft rubric. The professor answers
the single questionnaire all at once (`questionnaire.apply_answers`), then locks. No back-and-forth.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import pvariance
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import questionnaire as qmod
from .budget import Budget, BudgetExceeded
from .graders.claude_grader import GRADE_MAX_TOKENS, ClaudeGrader
from .ingest import apply_length_policy
from .llm import DEFAULT_CLAUDE_MODEL, SONNET_MODEL, StructuredLLM, estimate_cost
from .prompts import (ARCHETYPES, CRITIQUE_SYSTEM, GRADER_SYSTEM, QUESTIONNAIRE_SYSTEM, SYNTH_SYSTEM, SYNTH_USER,
                      prompt_hashes, render_question, render_rubric_all)
from .rubric import Rubric

# Expected outcome band (fraction of the question's max points) per archetype; None = no expectation.
EXPECTED_BAND: dict[str, tuple[float, float] | None] = {
    "strong": (0.75, 1.0), "median": (0.35, 0.8), "weak": (0.0, 0.4), "overlong": None,
    "rabbit_hole": (0.0, 0.5), "wrong_reasoning": (0.0, 0.6), "contrary_conclusion": (0.6, 1.0),
    "laundry_list": (0.0, 0.5), "wrong_jurisdiction": (0.0, 0.4), "terse_correct": (0.6, 1.0),
    "outside_syllabus": (0.0, 0.5),
}
# Most informative first. Degradation drops from the END; Claude stress grades go to the FRONT of this list.
ARCHETYPE_PRIORITY = ["strong", "median", "weak", "rabbit_hole", "overlong", "wrong_reasoning", "contrary_conclusion",
                      "laundry_list", "terse_correct", "wrong_jurisdiction", "outside_syllabus"]
assert set(ARCHETYPE_PRIORITY) == set(ARCHETYPES)
MIN_ARCHETYPES = 4
MIN_CLAUDE_GRADES = 3  # per question, before archetypes are cut
CRITIQUE_MAX_TOKENS = 8000
SYNTH_TOKEN_CAP = 32000
TOKENS_PER_WORD = 1.4
CHARS_PER_WORD = 6


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


class SynthAnswer(BaseModel):
    question_id: str
    archetype: str
    answer: str


class SynthAllOutput(BaseModel):
    answers: list[SynthAnswer]


@dataclass
class HardenConfig:
    n_archetypes: int | None = None  # None = scaled to the ask (see default_archetypes)
    k: int = 5  # OpenAI Decisions repeats per synthetic answer
    expected_words: int = 800
    spread_threshold: float = 1.0  # points
    concurrency: int = 4
    max_questions: int = 12
    use_decisions: bool = True
    critique_model: str = DEFAULT_CLAUDE_MODEL
    synth_model: str = SONNET_MODEL
    stress_model: str = SONNET_MODEL
    questionnaire_model: str = DEFAULT_CLAUDE_MODEL


def default_archetypes(rubric: Rubric, exam_text: str) -> int:
    """Scale to the ask: 11 for a one-question exam; fewer for multi-question exams, many criteria or a long exam."""
    n_crit = sum(len(q.criteria) for q in rubric.questions)
    n = 11
    if len(rubric.questions) > 1 or n_crit > 12:
        n = 8
    if len(exam_text) > 40000:
        n = min(n, 8)
    return n


@dataclass
class Plan:
    critique_model: str
    synth_model: str
    stress_model: str
    questionnaire_model: str
    archetypes: list[str]
    claude_grades_per_question: int
    k: int
    max_questions: int
    words_scale: float
    synth_max_tokens: int
    questionnaire_max_tokens: int
    estimate_usd: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)
    degradations: list[str] = field(default_factory=list)
    feasible: bool = True

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _answer_words(arch: str, expected: int, scale: float) -> int:
    base = expected * 5 // 2 if arch == "overlong" else (expected // 4 if arch == "terse_correct" else expected)
    return max(30, int(base * scale))


def _synth_specs(rubric: Rubric, archetypes: list[str], expected: int, scale: float) -> list[dict]:
    return [{"question_id": q.id, "archetype": a, "words": _answer_words(a, expected, scale)}
            for q in rubric.questions for a in archetypes]


def _synth_tokens(specs: list[dict]) -> int:
    return int(sum(s["words"] * TOKENS_PER_WORD + 40 for s in specs) + 500)


def plan(exam_text: str, syllabus_text: str, rubric: Rubric, budget: Budget, cfg: HardenConfig | None = None) -> Plan:
    """Size the run to the budget. Defaults: Opus critique, ONE Sonnet call for all synthetic answers, one Claude
    stress grade (Sonnet) per answer plus K Decisions repeats, one Opus questionnaire call.

    Estimates are worst case (see `Budget.estimate`). If they exceed `budget.remaining()` degrade in this order:
    fewer Claude stress grades (down to MIN_CLAUDE_GRADES per question), then fewer archetypes (down to MIN_ARCHETYPES),
    then Sonnet instead of Opus for the critique, then for the questionnaire, then no Claude stress grades at all.
    Every step is recorded in `Plan.degradations`; `feasible` is False if even the minimum does not fit.
    """
    cfg = cfg or HardenConfig()
    expected = rubric.policies.length_policy.word_cap or cfg.expected_words
    n = min(cfg.n_archetypes or default_archetypes(rubric, exam_text), len(ARCHETYPE_PRIORITY))
    p = Plan(cfg.critique_model, cfg.synth_model, cfg.stress_model, cfg.questionnaire_model,
             ARCHETYPE_PRIORITY[:n], n, cfg.k if cfg.use_decisions else 0, cfg.max_questions, 1.0, 0, 0)
    rubric_all = render_rubric_all(rubric)
    # one call must hold every synthetic answer: shorten them if the output cap would otherwise be exceeded
    while _synth_tokens(_synth_specs(rubric, p.archetypes, expected, p.words_scale)) > SYNTH_TOKEN_CAP and p.words_scale > 0.2:
        p.words_scale = round(p.words_scale * 0.8, 3)
    if p.words_scale < 1.0:
        p.degradations.append(f"synthetic answers shortened to {p.words_scale:.0%} of the target length so all fit in one call")

    def price() -> None:
        specs = _synth_specs(rubric, p.archetypes, expected, p.words_scale)
        p.synth_max_tokens = min(SYNTH_TOKEN_CAP, _synth_tokens(specs))
        p.questionnaire_max_tokens = min(16000, 800 * p.max_questions + 1000)
        n_ans = len(specs)
        crit_chars = len(CRITIQUE_SYSTEM) + len(exam_text) + len(syllabus_text) + len(rubric_all)
        avg_words = sum(s["words"] for s in specs) / max(1, n_ans)
        grade_chars = [len(GRADER_SYSTEM) + len(exam_text) + len(render_question(rubric, q)) + int(avg_words * CHARS_PER_WORD)
                       for q in rubric.questions]
        claude = sum(estimate_cost(p.stress_model, c, GRADE_MAX_TOKENS) for c in grade_chars) * p.claude_grades_per_question
        decisions = sum(estimate_cost("gpt-6-luna", c, 0) for c in grade_chars) * len(p.archetypes) * p.k
        q_chars = (len(QUESTIONNAIRE_SYSTEM) + len(rubric_all) + len(exam_text) + 6000 + n_ans * 80 * 4
                   + int(sum(min(s["words"] * CHARS_PER_WORD, 2500) for s in specs)))
        p.breakdown = {
            "critique": estimate_cost(p.critique_model, crit_chars, CRITIQUE_MAX_TOKENS),
            "synthetic_answers": estimate_cost(p.synth_model, len(SYNTH_SYSTEM) + len(exam_text) + len(syllabus_text) + 120 * n_ans,
                                               p.synth_max_tokens),
            "stress_claude": claude, "stress_decisions": decisions,
            "questionnaire": estimate_cost(p.questionnaire_model, q_chars, p.questionnaire_max_tokens),
        }
        p.estimate_usd = round(sum(p.breakdown.values()), 6)

    def fits() -> bool:
        price()
        return p.estimate_usd <= budget.remaining() + 1e-12

    if not fits():
        before = p.claude_grades_per_question
        while not fits() and p.claude_grades_per_question > min(MIN_CLAUDE_GRADES, len(p.archetypes)):
            p.claude_grades_per_question -= 1
        if p.claude_grades_per_question < before:
            p.degradations.append(f"fewer Claude stress grades: {p.claude_grades_per_question} of {before} answers per question")
    if not fits():
        before = len(p.archetypes)
        while not fits() and len(p.archetypes) > MIN_ARCHETYPES:
            p.archetypes = p.archetypes[:-1]
            p.claude_grades_per_question = min(p.claude_grades_per_question, len(p.archetypes))
        if len(p.archetypes) < before:
            p.degradations.append(f"fewer archetypes: {len(p.archetypes)} of {before} (dropped {', '.join(ARCHETYPE_PRIORITY[len(p.archetypes):before])})")
    if not fits() and p.critique_model != SONNET_MODEL:
        p.degradations.append(f"critique on {SONNET_MODEL} instead of {p.critique_model}")
        p.critique_model = SONNET_MODEL
    if not fits() and p.questionnaire_model != SONNET_MODEL:
        p.degradations.append(f"questionnaire on {SONNET_MODEL} instead of {p.questionnaire_model}")
        p.questionnaire_model = SONNET_MODEL
    if not fits() and p.claude_grades_per_question > 0:
        p.degradations.append("no Claude stress grades (Decisions repeats only; no evidence quotes)")
        p.claude_grades_per_question = 0
    p.feasible = fits()
    if not p.feasible:
        p.degradations.append(f"even the minimal plan (${p.estimate_usd:.2f}) exceeds the remaining budget (${budget.remaining():.2f})")
    return p


async def _gather(coros, limit: int):
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros))


def _pts_spread(vals: list[float]) -> float:
    return max(vals) - min(vals) if vals else 0.0


async def harden(rubric: Rubric, exam_text: str, syllabus_text: str, llm: StructuredLLM, *, decisions: Any | None = None,
                 budget: Budget | None = None, cfg: HardenConfig | None = None, run_id: str | None = None) -> dict:
    """Run one budgeted hardening pass and return the report (includes the Questionnaire as `report["questionnaire"]`).

    `llm`: any StructuredLLM; per-role models are derived with `llm.with_model(...)`. `decisions`: optional Decisions grader
    (built with allow_draft=True) used for the K variance repeats. Raises BudgetExceeded only if even the minimal plan
    cannot be afforded; otherwise degrades and records every degradation in the report.
    """
    cfg = cfg or HardenConfig(use_decisions=decisions is not None)
    budget = budget if budget is not None else Budget.unlimited()
    run_id = run_id or "harden-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pl = plan(exam_text, syllabus_text, rubric, budget, cfg)
    if not pl.feasible:
        raise BudgetExceeded(f"hardening needs about ${pl.estimate_usd:.2f} even at minimum size; remaining budget is "
                             f"${budget.remaining():.2f}", estimate_usd=pl.estimate_usd, remaining_usd=budget.remaining())
    degradations = list(pl.degradations)
    errors: list[str] = []
    expected = rubric.policies.length_policy.word_cap or cfg.expected_words
    crit_ids = [c.id for q in rubric.questions for c in q.criteria]
    if decisions is not None and hasattr(decisions, "allow_draft"):
        decisions.allow_draft = True  # stress-grading a draft is the point

    async def critique_call() -> CritiqueOutput | None:
        try:
            r = await llm.with_model(pl.critique_model).call(
                prefix=CRITIQUE_SYSTEM.format(exam=exam_text, syllabus=syllabus_text, rubric_all=render_rubric_all(rubric)),
                user="Review the draft rubric and report all findings.", schema=CritiqueOutput,
                max_tokens=CRITIQUE_MAX_TOKENS, effort="high", purpose="harden-critique", budget=budget,
                hint={"kind": "critique", "criterion_ids": crit_ids})
        except BudgetExceeded as e:
            degradations.append(f"critique skipped: {e}")
            return None
        if not r.ok:
            errors.append(f"critique: {r.error}")
            return None
        return r.parsed  # type: ignore[return-value]

    specs = _synth_specs(rubric, pl.archetypes, expected, pl.words_scale)

    async def synth_call() -> list[dict]:
        listing = "\n".join(f"{s['question_id']} | {s['archetype']} | ~{s['words']} words" for s in specs)
        try:
            r = await llm.with_model(pl.synth_model).call(
                prefix=SYNTH_SYSTEM.format(exam=exam_text, syllabus=syllabus_text),
                user=SYNTH_USER.format(archetype_defs="\n".join(f"- {a}: {ARCHETYPES[a]}" for a in pl.archetypes), specs=listing),
                schema=SynthAllOutput, max_tokens=pl.synth_max_tokens, effort="medium", purpose="harden-synthetic",
                budget=budget, hint={"kind": "synth_all", "specs": specs})
        except BudgetExceeded as e:
            degradations.append(f"synthetic answers skipped: {e}")
            return []
        if not r.ok:
            errors.append(f"synthetic answers: {r.error}")
            return []
        got = {(a.question_id, a.archetype): a.answer for a in r.parsed.answers}  # type: ignore[attr-defined]
        out = []
        for s in specs:
            text = got.get((s["question_id"], s["archetype"]))
            if text:
                out.append({"id": f"SYN{len(out) + 1:03d}", "question_id": s["question_id"], "archetype": s["archetype"], "text": text})
        if len(out) < len(specs):
            degradations.append(f"model returned {len(out)} of {len(specs)} requested synthetic answers")
        return out

    critique, answers = await asyncio.gather(critique_call(), synth_call())

    # (c) stress grading: 1 Claude grade (evidence quotes) for the priority archetypes + K Decisions repeats for all
    claude = ClaudeGrader(llm.with_model(pl.stress_model), allow_draft=True, retry_delay=0, purpose="harden-stress-claude")
    claude_set = set(ARCHETYPE_PRIORITY[:pl.claude_grades_per_question])
    skipped = {"budget": 0, "error": 0}

    async def grade_once(a: dict, grader: Any, rep: int) -> dict | None:
        text, info = apply_length_policy(a["text"], rubric.policies.length_policy)
        try:
            r = await grader.grade(rubric, rubric.question(a["question_id"]), exam_text, text, budget=budget)
        except BudgetExceeded:
            skipped["budget"] += 1
            return None
        except Exception as e:  # noqa: BLE001 - a failed stress grade must not abort hardening
            skipped["error"] += 1
            errors.append(f"stress grade {a['id']} ({grader.name}): {type(e).__name__}: {e}"[:300])
            return None
        if r.error:
            skipped["error"] += 1
            errors.append(f"stress grade {a['id']} ({grader.name}): {r.error}"[:300])
            return None
        return {"answer_id": a["id"], "grader": grader.name, "rep": rep, "deduction": info["deduction"],
                "points": {g.criterion_id: g.points for g in r.grades}, "labels": {g.criterion_id: g.label for g in r.grades},
                "evidence": {g.criterion_id: g.evidence for g in r.grades}}

    jobs = []
    for a in answers:
        if a["archetype"] in claude_set and pl.claude_grades_per_question > 0:
            jobs.append(grade_once(a, claude, 0))
        if decisions is not None and pl.k > 0:
            jobs += [grade_once(a, decisions, k) for k in range(pl.k)]
    runs = [r for r in await _gather(jobs, cfg.concurrency) if r]
    if skipped["budget"]:
        degradations.append(f"budget exhausted mid-run: {skipped['budget']} stress grade(s) skipped")
    if skipped["error"]:
        degradations.append(f"{skipped['error']} stress grade(s) failed (see errors)")

    flags, stress_rows = [], []
    crit_spread: dict[str, float] = {}
    answer_gap: dict[str, float] = {}
    for a in answers:
        q = rubric.question(a["question_id"])
        mine = [r for r in runs if r["answer_id"] == a["id"]]
        if not mine:
            continue
        totals = [sum(r["points"].values()) - r["deduction"] for r in mine]
        frac = (sum(totals) / len(totals)) / q.max_points
        band = EXPECTED_BAND.get(a["archetype"])
        row = {"answer_id": a["id"], "question_id": q.id, "archetype": a["archetype"], "words": len(a["text"].split()),
               "grades": len(mine), "mean_fraction": round(frac, 3), "total_spread": round(_pts_spread(totals), 3),
               "expected_band": band, "criteria": []}
        if band and not (band[0] - 1e-9 <= frac <= band[1] + 1e-9):
            gap = band[0] - frac if frac < band[0] else frac - band[1]
            answer_gap[a["id"]] = max(answer_gap.get(a["id"], 0.0), gap)
            flags.append({"kind": "archetype_outcome", "answer_id": a["id"], "question_id": q.id, "archetype": a["archetype"],
                          "mean_fraction": row["mean_fraction"], "expected_band": band,
                          "issue": f"{a['archetype']} answer {a['id']} scored {frac:.0%} of {q.max_points:g} pts; expected {band[0]:.0%}-{band[1]:.0%}."})
        for c in q.criteria:
            pts = [r["points"][c.id] for r in mine]
            labs = {r["labels"][c.id] for r in mine}
            spread = _pts_spread(pts)
            unstable = spread > cfg.spread_threshold + 1e-9 or (c.kind != "score" and len(labs) > 1)
            crit_spread[c.id] = max(crit_spread.get(c.id, 0.0), spread / c.max_points)
            quotes = next((r["evidence"][c.id] for r in mine if r["grader"] == "claude" and r["evidence"].get(c.id)), [])
            row["criteria"].append({"criterion_id": c.id, "points": pts, "spread": spread, "variance": round(pvariance(pts), 4),
                                    "unstable": unstable, "claude_evidence": quotes})
            if unstable:
                flags.append({"kind": "unstable", "answer_id": a["id"], "question_id": q.id, "criterion_id": c.id,
                              "archetype": a["archetype"], "points": pts, "spread": spread,
                              "issue": f"Criterion {c.id} scored {pts} across {len(pts)} grades of {a['archetype']} answer {a['id']}."})
        stress_rows.append(row)
    for i, f in enumerate(flags, 1):
        f["flag_id"] = f"S{i}"
    findings = [{"id": f"C{i}", "category": f.category, "criterion_id": f.criterion_id, "issue": f.issue,
                 "evidence": f.evidence, "amendment": f.amendment.model_dump()}
                for i, f in enumerate(critique.findings if critique else [], 1)]

    # (d) ONE questionnaire
    qn = await _questionnaire(rubric, exam_text, llm, pl, budget, cfg, run_id, findings, flags, stress_rows, answers,
                              crit_spread, answer_gap, degradations, errors)

    return {
        "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exam_id": rubric.exam_id, "rubric_version": rubric.version, "draft_rubric_hash": rubric.compute_hash(),
        "plan": pl.to_dict(), "degradations": degradations, "errors": errors,
        "config": cfg.__dict__, "prompt_hashes": prompt_hashes(),
        "critique_findings": findings, "flags": flags, "stress": stress_rows, "synthetic_answers": answers,
        "questionnaire": qn.model_dump(), "budget": budget.summary(),
        "note": "Synthetic answers are fictional. The professor answers the questionnaire once, runs `essay-grader apply-answers`, then `lock`.",
    }


async def _questionnaire(rubric, exam_text, llm, pl: Plan, budget: Budget, cfg: HardenConfig, run_id, findings, flags,
                         stress_rows, answers, crit_spread, answer_gap, degradations, errors) -> qmod.Questionnaire:
    texts = {a["id"]: a["text"] for a in answers}
    user = ("<critique_findings>\n" + json.dumps(findings, indent=1) + "\n</critique_findings>\n\n"
            "<stress_results>\n" + json.dumps(
                [{"answer_id": r["answer_id"], "question_id": r["question_id"], "archetype": r["archetype"],
                  "mean_pct_of_max": round(r["mean_fraction"] * 100), "expected_band": r["expected_band"],
                  "total_spread_pts": r["total_spread"],
                  "criteria": [{"criterion_id": c["criterion_id"], "points": c["points"],
                               "mean_points": _mean_points(c["points"]), "spread_pts": c["spread"]} for c in r["criteria"]]}
                 for r in stress_rows], indent=0) + "\n</stress_results>\n\n"
            "<synthetic_answers>\n" + "\n".join(
                f'<answer id="{a["id"]}" question_id="{a["question_id"]}" archetype="{a["archetype"]}">\n{a["text"][:2500]}\n</answer>'
                for a in answers) + "\n</synthetic_answers>\n\nWrite the questionnaire.")
    prefix = QUESTIONNAIRE_SYSTEM.format(max_questions=cfg.max_questions, exam=exam_text, rubric_all=render_rubric_all(rubric))
    observed = {r["answer_id"]: {c["criterion_id"]: _mean_points(c["points"]) for c in r["criteria"]} for r in stress_rows}
    hint = {"kind": "questionnaire", "criterion_ids": [c.id for q in rubric.questions for c in q.criteria],
            "criteria_max": {c.id: c.max_points for q in rubric.questions for c in q.criteria}, "observed_points": observed,
            "answers": [{"id": a["id"], "archetype": a["archetype"]} for a in answers], "max_questions": cfg.max_questions}
    raw: list[qmod.RawQuestion] | None = None
    used_model = None
    models = [pl.questionnaire_model] + ([SONNET_MODEL] if pl.questionnaire_model != SONNET_MODEL else [])
    for m in models:
        try:
            r = await llm.with_model(m).call(prefix=prefix, user=user, schema=qmod.QuestionnaireOutput,
                                             max_tokens=pl.questionnaire_max_tokens, effort="high",
                                             purpose="harden-questionnaire", budget=budget, hint=hint)
        except BudgetExceeded as e:
            degradations.append(f"questionnaire on {m} not affordable: {e}")
            continue
        if not r.ok:
            errors.append(f"questionnaire ({m}): {r.error}")
            break  # a refusal will not improve on a cheaper model
        raw, used_model = r.parsed.questions, m  # type: ignore[attr-defined]
        if m != pl.questionnaire_model:
            degradations.append(f"questionnaire on {m} instead of {pl.questionnaire_model} (budget)")
        break
    warnings: list[str] = []
    if raw is None:
        raw = fallback_questions(rubric, findings)
        warnings.append("Questionnaire model unavailable (budget, refusal or error): built mechanically from the critique "
                        "findings only; stress-test outcome questions are omitted. See report for flags.")
        degradations.append("questionnaire built without an LLM call (fallback from critique findings)")
    return qmod.build_questionnaire(rubric, raw, run_id=run_id, max_questions=cfg.max_questions, crit_spread=crit_spread,
                                    answer_gap=answer_gap, answer_texts=texts, model=used_model, id_prefix="H",
                                    warnings=warnings, observed_points=observed)


def _mean_points(pts: list[float]) -> float:
    return round(sum(pts) / len(pts), 2) if pts else 0.0


def fallback_questions(rubric: Rubric, findings: list[dict]) -> list[qmod.RawQuestion]:
    """No-LLM questionnaire: each critique finding becomes adopt / keep-as-written."""
    out = []
    for f in findings:
        a = f["amendment"]
        cid = f["criterion_id"] or a.get("criterion_id")
        if a["type"] == "add_criterion":
            continue  # structural change; not expressible as a ruling
        if a["type"] == "reword_descriptor" and cid:
            adopt = qmod.Option(label="Adopt amendment", rewrite=qmod.Rewrite(target="description", text=a["text"]))
        elif cid:
            adopt = qmod.Option(label="Adopt amendment", ruling=a["text"])
        else:
            adopt = qmod.Option(label="Adopt amendment", ruling=a["text"])
        out.append(qmod.RawQuestion(
            criterion_id=cid, scenario=f["issue"], why_it_matters=f"Critique finding ({f['category']}): {f['evidence']}",
            severity=3, options=[adopt, qmod.Option(label="Keep as written")], recommended="Adopt amendment"))
    return out


def render_markdown(rep: dict) -> str:
    pl = rep["plan"]
    L = [f"# Rubric hardening report: {rep['exam_id']} v{rep['rubric_version']}", "",
         f"- Run `{rep['run_id']}` - draft rubric hash `{rep['draft_rubric_hash'][:16]}` - generated {rep['generated_at']}",
         f"- Next: answer **questionnaire.md** once (all questions), `essay-grader apply-answers`, then `essay-grader lock`.", "",
         "## Plan", "",
         f"- Critique: `{pl['critique_model']}`; synthetic answers (one call): `{pl['synth_model']}` x {len(pl['archetypes'])} archetypes per question; "
         f"stress grading: {pl['claude_grades_per_question']} Claude grade(s) per question on `{pl['stress_model']}` + {pl['k']} Decisions repeats each; "
         f"questionnaire: `{pl['questionnaire_model']}`.",
         f"- Estimated (worst case) ${pl['estimate_usd']:.2f}; actual ${rep['budget']['spent_usd']:.2f}"
         + (f" of ${rep['budget']['max_usd']:.2f}" if rep['budget']['max_usd'] is not None else " (no cap)") + ".", ""]
    L += ["### Degradations", ""] + ([f"- {d}" for d in rep["degradations"]] or ["None."]) + [""]
    if rep["errors"]:
        L += ["### Errors", ""] + [f"- {e}" for e in rep["errors"]] + [""]
    L += ["## Archetype outcomes (mean over all grades)", "",
          "| Answer | Question | Archetype | Words | Grades | Mean % of max | Expected | Total spread |", "|---|---|---|---|---|---|---|---|"]
    for r in rep["stress"]:
        b = r["expected_band"]
        exp = f"{b[0]:.0%}-{b[1]:.0%}" if b else "n/a"
        bad = " **(!)**" if b and not (b[0] <= r["mean_fraction"] <= b[1]) else ""
        L.append(f"| {r['answer_id']} | {r['question_id']} | {r['archetype']} | {r['words']} | {r['grades']} | {r['mean_fraction']:.0%}{bad} | {exp} | {r['total_spread']} |")
    L += ["", "## Critique findings (merged into the questionnaire)", ""]
    for f in rep["critique_findings"] or []:
        L += [f"- **{f['id']}** [{f['category']}] {f['criterion_id'] or '-'}: {f['issue']}"]
    if not rep["critique_findings"]:
        L.append("None.")
    L += ["", "## Spend by call", ""] + [f"- {u['purpose']} / {u['model']}: {u['calls']} call(s), ${u['cost_usd']:.4f}" for u in rep["budget"]["by_purpose"]]
    return "\n".join(L) + "\n"


def write_report(rep: dict, out_dir: str | Path) -> Path:
    d = Path(out_dir) / rep["run_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "harden_report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (d / "harden_report.md").write_text(render_markdown(rep), encoding="utf-8")
    qn = qmod.Questionnaire.model_validate(rep["questionnaire"])
    (d / "questionnaire.json").write_text(qn.model_dump_json(indent=2), encoding="utf-8")
    (d / "questionnaire.md").write_text(qmod.render_markdown(qn), encoding="utf-8")
    return d
