"""One-shot professor questionnaire: schema, ranking, dedupe, markdown rendering, and `apply_answers`.

Hardening and calibration both emit a `Questionnaire`; the professor answers every question at once (question id ->
option label or custom text); `apply_answers` writes the resulting rulings / descriptor rewrites into a NEW draft rubric
with provenance. The result is ready to `lock()`.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .rubric import Provenance, Rubric

MIN_SPREAD = 0.1  # floor for the "observed spread" factor so critique-only findings still rank
POLICY_FIELDS = ("off_topic_policy", "alternative_conclusions_policy", "citation_policy")
DEDUPE_JACCARD = 0.6


class Rewrite(BaseModel):
    target: str = Field(description='"description", "level:<label>", "anchor:<value>" (criterion) or "policy:<field>" (rubric-level).')
    text: str = Field(description="The exact replacement text.")


class ProjectedEffect(BaseModel):
    """The model's ESTIMATE of what adopting an option does to the sample answer that triggered the question."""
    criterion_id: str
    before_points: float = Field(description="That sample answer's points on the criterion today (the stress-test mean).")
    after_points: float = Field(description="Estimated points on the same criterion if this option is adopted.")


class Option(BaseModel):
    label: str
    ruling: str | None = Field(default=None, description="Exact ruling text appended to the criterion (or rubric-wide).")
    rewrite: Rewrite | None = Field(default=None, description="Descriptor rewrite; set this OR ruling, not both. "
                                    "Neither set means 'keep the rubric as written'.")
    projected: list[ProjectedEffect] | None = Field(
        default=None, description="Optional AI projection of this option's effect on the triggering synthetic answer's criterion scores. "
        "Absent on questionnaires written before this field existed, and on calibration questionnaires.")

    @property
    def applied_text(self) -> str:
        return self.rewrite.text if self.rewrite else (self.ruling or "")


class RawQuestion(BaseModel):
    """What the LLM returns (calibration and hardening); code ranks, validates and dedupes."""
    criterion_id: str | None = Field(default=None, description="null for a rubric-level policy question.")
    scenario: str = Field(description="Concrete situation that makes the rubric ambiguous or wrong.")
    excerpt: str = Field(default="", description="Short VERBATIM excerpt from the cited synthetic answer.")
    answer_id: str | None = Field(default=None, description="Id of the synthetic answer that triggered this.")
    why_it_matters: str = Field(description="Observed spread or pedagogically wrong outcome.")
    severity: int = Field(default=3, description="1 (minor) to 5 (pedagogically wrong outcome).")
    options: list[Option]
    recommended: str = Field(description="Label of the recommended option.")


class QuestionnaireOutput(BaseModel):
    questions: list[RawQuestion]


class Question(BaseModel):
    id: str
    criterion_id: str | None = None  # None = rubric-level policy
    scenario: str
    excerpt: str = ""
    answer_id: str | None = None
    why_it_matters: str
    options: list[Option]
    recommended: str
    allow_custom: bool = True
    severity: int = 3
    impact: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> "Question":
        if not 2 <= len(self.options) <= 4:
            raise ValueError(f"{self.id}: needs 2-4 options")
        labels = [o.label for o in self.options]
        if len(set(labels)) != len(labels):
            raise ValueError(f"{self.id}: duplicate option labels")
        if self.recommended not in labels:
            raise ValueError(f"{self.id}: recommended {self.recommended!r} is not an option label")
        for o in self.options:
            if o.ruling is not None and o.rewrite is not None:
                raise ValueError(f"{self.id}/{o.label}: set ruling or rewrite, not both")
        return self

    def option(self, label: str) -> Option | None:
        want = _norm(label)
        return next((o for o in self.options if _norm(o.label) == want), None)


class Questionnaire(BaseModel):
    schema_version: int = 1
    run_id: str
    run_kind: Literal["hardening", "calibration"] = "hardening"
    exam_id: str
    rubric_version: int
    draft_rubric_hash: str
    max_questions: int = 12
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    model: str | None = None
    questions: list[Question] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


# ---------------------------------------------------------------------------------------------------------------------
# validation against the rubric


def _find_criterion(rubric: Rubric, cid: str):
    for q in rubric.questions:
        for c in q.criteria:
            if c.id == cid:
                return c
    return None


def check_option(rubric: Rubric, criterion_id: str | None, o: Option) -> str | None:
    """Return a problem description, or None if the option can be applied to this rubric."""
    if o.ruling is not None and o.rewrite is not None:
        return "set ruling or rewrite, not both"
    if o.ruling is None and o.rewrite is None:
        return None  # "keep as written"
    if o.ruling is not None:
        return None if o.ruling.strip() else "empty ruling"
    rw = o.rewrite
    assert rw is not None
    if not rw.text.strip():
        return "empty rewrite text"
    if criterion_id is None:
        return None if rw.target in {f"policy:{f}" for f in POLICY_FIELDS} else f"bad policy target {rw.target!r}"
    c = _find_criterion(rubric, criterion_id)
    if c is None:
        return f"unknown criterion {criterion_id!r}"
    if rw.target == "description":
        return None
    kind, _, key = rw.target.partition(":")
    if kind == "level" and any(lv.label == key for lv in c.levels):
        return None
    if kind == "anchor" and c.scale:
        try:
            if any(abs(a.value - float(key)) < 1e-9 for a in c.scale.anchors):
                return None
        except ValueError:
            pass
    return f"rewrite target {rw.target!r} does not exist on criterion {criterion_id}"


def clean_projected(rubric: Rubric, o: Option, observed: dict[str, float] | None = None,
                    warns: list[str] | None = None) -> Option:
    """Validate an option's projections against the rubric and clamp them to each criterion's range (0..max_points).

    Unknown criteria and non-finite numbers are dropped (with a warning); one entry per criterion (the first wins). When the
    stress test observed the triggering answer's points on a criterion (`observed`: criterion id -> mean points),
    `before_points` is replaced by that measured value, so only `after_points` is the model's estimate.
    """
    if not o.projected:
        return o.model_copy(update={"projected": None})
    out: list[ProjectedEffect] = []
    seen: set[str] = set()
    for e in o.projected:
        c = _find_criterion(rubric, e.criterion_id)
        if c is None:
            if warns is not None:
                warns.append(f"dropped projection for unknown criterion {e.criterion_id!r} (option {o.label!r})")
            continue
        if e.criterion_id in seen:
            continue
        before, after = e.before_points, e.after_points
        if observed and e.criterion_id in observed:
            before = observed[e.criterion_id]
        if not (math.isfinite(before) and math.isfinite(after)):
            if warns is not None:
                warns.append(f"dropped non-numeric projection for {e.criterion_id!r} (option {o.label!r})")
            continue
        seen.add(e.criterion_id)
        out.append(ProjectedEffect(criterion_id=e.criterion_id, before_points=round(min(c.max_points, max(0.0, before)), 2),
                                   after_points=round(min(c.max_points, max(0.0, after)), 2)))
    return o.model_copy(update={"projected": out or None})


# ---------------------------------------------------------------------------------------------------------------------
# ranking / dedupe / cap


def impact_of(rubric: Rubric, rq: RawQuestion, crit_spread: dict[str, float], answer_gap: dict[str, float]) -> float:
    """impact = criterion weight x observed spread x severity.

    weight: criterion max points / total rubric points (1.0 for rubric-level policy questions);
    spread: max(criterion point spread as a fraction of the criterion's max, outcome gap of the cited answer as a
    fraction of the question max), floored at MIN_SPREAD; severity: 1..5 (clamped).
    """
    total = sum(q.max_points for q in rubric.questions) or 1.0
    c = _find_criterion(rubric, rq.criterion_id) if rq.criterion_id else None
    weight = c.max_points / total if c else 1.0
    spread = max(MIN_SPREAD, crit_spread.get(rq.criterion_id or "", 0.0), answer_gap.get(rq.answer_id or "", 0.0))
    return round(weight * spread * min(5, max(1, rq.severity)), 6)


def build_questionnaire(rubric: Rubric, raw: list[RawQuestion], *, run_id: str, run_kind: str = "hardening",
                        max_questions: int = 12, crit_spread: dict[str, float] | None = None,
                        answer_gap: dict[str, float] | None = None, answer_texts: dict[str, str] | None = None,
                        model: str | None = None, id_prefix: str = "H", warnings: list[str] | None = None,
                        draft_hash: str | None = None,
                        observed_points: dict[str, dict[str, float]] | None = None) -> Questionnaire:
    """Validate, score, dedupe, rank and cap raw questions into a Questionnaire.

    `observed_points` (answer id -> criterion id -> mean stress-test points) anchors each projection's `before_points` to what
    was measured. Calibration questionnaires carry no projections (their questions are about general approaches, not one answer).
    """
    crit_spread, answer_gap, answer_texts = crit_spread or {}, answer_gap or {}, answer_texts or {}
    observed_points = observed_points or {}
    warns = list(warnings or [])
    scored: list[tuple[float, RawQuestion]] = []
    for rq in raw:
        if rq.criterion_id and _find_criterion(rubric, rq.criterion_id) is None:
            warns.append(f"dropped question on unknown criterion {rq.criterion_id!r}")
            continue
        opts, seen = [], set()
        for o in rq.options:
            prob = check_option(rubric, rq.criterion_id, o)
            if prob:
                warns.append(f"dropped option {o.label!r} ({prob})")
            elif _norm(o.label) not in seen:
                seen.add(_norm(o.label))
                opts.append(o.model_copy(update={"projected": None}) if run_kind == "calibration"
                            else clean_projected(rubric, o, observed_points.get(rq.answer_id or ""), warns))
        opts = opts[:4]
        if len(opts) < 2:
            warns.append(f"dropped question {rq.scenario[:60]!r}: fewer than 2 valid options")
            continue
        rec = rq.recommended if any(o.label == rq.recommended for o in opts) else opts[0].label
        if rec != rq.recommended:
            warns.append(f"recommended option {rq.recommended!r} invalid; defaulted to {rec!r}")
        # the excerpt must be verbatim from the cited synthetic answer; otherwise use a verbatim opening slice of it
        excerpt = rq.excerpt.strip()
        src = answer_texts.get(rq.answer_id or "")
        if src is not None and (not excerpt or _norm(excerpt) not in _norm(src)):
            excerpt = " ".join(src.split())[:200]
        clean = rq.model_copy(update={"options": opts, "recommended": rec, "excerpt": excerpt})
        scored.append((impact_of(rubric, clean, crit_spread, answer_gap), clean))

    scored.sort(key=lambda t: -t[0])
    kept: list[tuple[float, RawQuestion]] = []
    for imp, rq in scored:  # higher impact first, so the duplicate that is dropped is the weaker one
        toks = _tokens(rq.scenario + " " + rq.why_it_matters)
        dup = next((k for _, k in kept if k.criterion_id == rq.criterion_id
                    and _jaccard(toks, _tokens(k.scenario + " " + k.why_it_matters)) >= DEDUPE_JACCARD), None)
        if dup is not None:
            warns.append(f"merged near-duplicate question on {rq.criterion_id or 'policy'}: {rq.scenario[:60]!r}")
            continue
        kept.append((imp, rq))
    if len(kept) > max_questions:
        warns.append(f"{len(kept) - max_questions} lower-impact question(s) omitted (max_questions={max_questions})")
        kept = kept[:max_questions]
    questions = [Question(id=f"{id_prefix}{i}", criterion_id=rq.criterion_id, scenario=rq.scenario,
                          excerpt=rq.excerpt, answer_id=rq.answer_id, why_it_matters=rq.why_it_matters,
                          options=rq.options, recommended=rq.recommended, allow_custom=True,
                          severity=min(5, max(1, rq.severity)), impact=imp)
                 for i, (imp, rq) in enumerate(kept, 1)]
    return Questionnaire(run_id=run_id, run_kind=run_kind, exam_id=rubric.exam_id, rubric_version=rubric.version,
                         draft_rubric_hash=draft_hash or rubric.compute_hash(), max_questions=max_questions,
                         model=model, questions=questions, warnings=warns)


# ---------------------------------------------------------------------------------------------------------------------
# rendering


def render_markdown(qn: Questionnaire) -> str:
    L = [f"# Rubric questionnaire: {qn.exam_id} v{qn.rubric_version}", "",
         f"Run `{qn.run_id}` ({qn.run_kind}). {len(qn.questions)} question(s), ranked by impact "
         "(criterion weight x observed spread x severity).", "",
         "Answer **all** questions in one pass; nothing else will be asked. Write the option label (exactly) or your own "
         "ruling text in `answers.yaml` (template at the end). Skipped questions get the recommended option. Then run "
         "`essay-grader apply-answers`, `essay-grader lock`, and grade. Scenarios use fictional (synthetic) answers.", ""]
    for q in qn.questions:
        L += [f"## {q.id}. {q.criterion_id or 'Rubric-level policy'}  (impact {q.impact:.3f}, severity {q.severity}/5)", "",
              f"**Scenario:** {q.scenario}", ""]
        if q.excerpt:
            L += [f"> {q.excerpt}", ""]
        L += [f"**Why it matters:** {q.why_it_matters}", ""]
        for o in q.options:
            star = " (recommended)" if o.label == q.recommended else ""
            what = (f"rewrite `{o.rewrite.target}` to: {o.rewrite.text}" if o.rewrite
                    else f"ruling: {o.ruling}" if o.ruling else "keep the rubric as written (no change)")
            proj = ""
            if o.projected:
                proj = " Projected by AI on the sample answer: " + "; ".join(
                    f"{p.criterion_id} {p.before_points:g} -> {p.after_points:g}" for p in o.projected) + "."
            L.append(f"- **{o.label}**{star}: {what}{proj}")
        L += ["- *Or write your own ruling text.*", ""]
    if qn.warnings:
        L += ["## Notes", ""] + [f"- {w}" for w in qn.warnings] + [""]
    L += ["## answers.yaml template", "", "```yaml"]
    for q in qn.questions:
        L.append(f"{q.id}: {_yaml_str(q.recommended)}")
    L += ["```", ""]
    return "\n".join(L)


def _yaml_str(s: str) -> str:
    import json

    return json.dumps(s, ensure_ascii=False)


# ---------------------------------------------------------------------------------------------------------------------
# applying answers


def _resolve(q: Question, ans: Any) -> tuple[Option, str, str | None]:
    """-> (option to apply, source, option_label). Custom text becomes a synthetic ruling Option."""
    if isinstance(ans, dict):
        ans = ans.get("option") or ans.get("custom")
    if ans is None or (isinstance(ans, str) and not ans.strip()):
        return q.option(q.recommended), "recommended_default", q.recommended  # type: ignore[return-value]
    text = str(ans).strip()
    o = q.option(text)
    if o is not None:
        return o, "answer", o.label
    if not q.allow_custom:
        raise ValueError(f"{q.id}: {text!r} is not one of the options and custom answers are not allowed")
    return Option(label="custom", ruling=text), "custom", None


def apply_answers(rubric: Rubric, questionnaire: Questionnaire, answers: dict[str, Any], *,
                  allow_stale: bool = False) -> Rubric:
    """New UNLOCKED rubric (version + 1) with every question's ruling/rewrite applied and recorded in `provenance`.

    `answers` maps question id -> option label, or custom ruling text. Unanswered questions apply the recommended option
    (provenance source "recommended_default"). Raises ValueError for unknown question ids or a stale questionnaire.
    """
    if not allow_stale and questionnaire.draft_rubric_hash != rubric.compute_hash():
        raise ValueError("questionnaire was generated for a different rubric (hash mismatch); re-run hardening or pass "
                         "allow_stale=True")
    qids = {q.id for q in questionnaire.questions}
    unknown = sorted(set(answers) - qids)
    if unknown:
        raise ValueError(f"answers for unknown question ids: {unknown}")
    out = rubric.model_copy(deep=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for q in questionnaire.questions:
        opt, source, label = _resolve(q, answers.get(q.id))
        problem = check_option(out, q.criterion_id, opt)
        if problem:
            raise ValueError(f"{q.id}: cannot apply option {opt.label!r}: {problem}")
        crit = _find_criterion(out, q.criterion_id) if q.criterion_id else None
        before = None
        if opt.rewrite is None and not opt.ruling:
            pass  # "keep as written": recorded in provenance, rubric text unchanged
        elif opt.rewrite is not None:
            before = _rewrite(out, crit, opt.rewrite)
        elif crit is not None:
            crit.edge_case_rulings.append(opt.ruling)  # type: ignore[arg-type]
        else:
            out.policies.additional_rulings.append(opt.ruling)  # type: ignore[arg-type]
        out.provenance.append(Provenance(
            hardening_run_id=questionnaire.run_id, question_id=q.id, run_kind=questionnaire.run_kind,
            criterion_id=q.criterion_id,
            action="rewrite_descriptor" if opt.rewrite else "ruling" if opt.ruling else "no_change",
            target=opt.rewrite.target if opt.rewrite else None, text=opt.applied_text, before=before,
            option_label=label, source=source, applied_at=now))  # type: ignore[arg-type]
    out.version = rubric.version + 1
    out.locked, out.content_hash, out.locked_at = False, None, None
    return out


def _rewrite(r: Rubric, crit, rw: Rewrite) -> str | None:
    if crit is None:
        field_ = rw.target.split(":", 1)[1]
        before = getattr(r.policies, field_)
        setattr(r.policies, field_, rw.text)
        return before
    if rw.target == "description":
        before, crit.description = crit.description, rw.text
        return before
    kind, _, key = rw.target.partition(":")
    if kind == "level":
        lv = next(lv for lv in crit.levels if lv.label == key)
        before, lv.descriptor = lv.descriptor, rw.text
        return before
    an = next(a for a in crit.scale.anchors if abs(a.value - float(key)) < 1e-9)
    before, an.descriptor = an.descriptor, rw.text
    return before
