"""Cross-grader agreement: per-answer disagreement flags, suggested scores, run-level statistics."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import mean

from .graders.base import GraderResult
from .rubric import Question, Rubric


@dataclass
class AgreementConfig:
    disagreement_threshold: float = 1.0  # points; score criteria flag when |diff| > this
    low_confidence: float = 0.5  # Claude confidence below this flags the criterion
    combine: str = "mean"  # mean | min | max


def combine_points(vals: list[float], how: str) -> float:
    return {"mean": mean, "min": min, "max": max}[how](vals)


def cohen_kappa(a: list, b: list) -> float | None:
    """Cohen's kappa for two raters over categorical labels. None when undefined (chance agreement == 1)."""
    n = len(a)
    if n == 0 or n != len(b):
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe >= 1.0:
        return None
    return (po - pe) / (1 - pe)


def evaluate(question: Question, results: dict[str, GraderResult], cfg: AgreementConfig) -> dict:
    """Compare independent grader outputs for one (student, question)."""
    flags: list[str] = []
    ok = {n: r for n, r in results.items() if r.error is None}
    for n, r in results.items():
        if r.error:
            flags.append(f"grader_error:{n}")
    criteria = []
    for c in question.criteria:
        g = {n: next(x for x in r.grades if x.criterion_id == c.id) for n, r in ok.items()}
        cflags: list[str] = []
        pts = {n: x.points for n, x in g.items()}
        if len(g) >= 2:
            vals = list(g.values())
            if c.kind == "score":
                dis = abs(vals[0].points - vals[1].points) > cfg.disagreement_threshold + 1e-9
            else:  # binary / choice: any label mismatch
                dis = vals[0].label != vals[1].label
            if dis:
                cflags.append("disagree")
        for n, x in g.items():
            if x.invalid_quotes:
                cflags.append(f"invalid_quotes:{n}")
            if x.confidence is not None and n == "claude" and x.confidence < cfg.low_confidence:
                cflags.append(f"low_confidence:{n}")
        suggested = None
        if len(g) >= 2 and "disagree" not in cflags:
            suggested = combine_points(list(pts.values()), cfg.combine)
        elif len(g) == 1:
            suggested = next(iter(pts.values()))
        criteria.append({"criterion_id": c.id, "points": pts, "labels": {n: x.label for n, x in g.items()},
                         "flags": cflags, "suggested": suggested})
        flags += [f"{f}@{c.id}" for f in cflags]
    n_ok = len(ok)
    if n_ok == 0 or any(f.startswith("grader_error") for f in flags):
        status = "needs_human_review"
    elif flags:
        status = "needs_human_review"
    elif len(results) == 1:
        status = "single_grader"  # nothing to cross-check; still only a suggestion
    else:
        status = "agreed"
    all_sugg = all(c["suggested"] is not None for c in criteria)
    total = sum(c["suggested"] for c in criteria) if (all_sugg and status in ("agreed", "single_grader")) else None
    return {"criteria": criteria, "flags": flags, "status": status, "suggested_total": total}


def run_stats(records: list[dict], rubric: Rubric) -> list[dict]:
    """Per-criterion agreement across the run (only records where both graders produced grades)."""
    rows = []
    for q in rubric.questions:
        for c in q.criteria:
            pa, pb, la, lb = [], [], [], []
            for rec in records:
                if rec["question_id"] != q.id:
                    continue
                crit = next((x for x in rec.get("criteria", []) if x["criterion_id"] == c.id), None)
                if not crit or len(crit["points"]) < 2:
                    continue
                (n1, n2) = list(crit["points"])[:2]
                pa.append(crit["points"][n1]); pb.append(crit["points"][n2])
                la.append(crit["labels"].get(n1)); lb.append(crit["labels"].get(n2))
            n = len(pa)
            row = {"question_id": q.id, "criterion_id": c.id, "kind": c.kind, "n": n,
                   "exact_agreement": None, "kappa": None, "mean_abs_diff": None}
            if n:
                if c.kind == "score":
                    row["exact_agreement"] = sum(abs(x - y) < 1e-9 for x, y in zip(pa, pb)) / n
                else:
                    row["exact_agreement"] = sum(x == y for x, y in zip(la, lb)) / n
                    row["kappa"] = cohen_kappa(la, lb)
                row["mean_abs_diff"] = mean(abs(x - y) for x, y in zip(pa, pb))
            rows.append(row)
    return rows
