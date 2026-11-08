"""Scoring return intake and analysis, following exam-taker's pre-registered analysis plan (methodology.md section 8).

Statistics produced (and nothing else; no new tests are invented):
  8.2  per-AI-ID raw score, z-score against the REAL-STUDENT-ONLY distribution (sample SD, n-1 divisor), rank (1-indexed,
       smaller = better, ties share the better rank), and percentile
  8.3  arm contrasts: raw effect (later - earlier arm), standardized effect (z difference), rank effect (rank difference,
       negative = the later arm ranks better); A vs S vs B from the design's arm order (S-A, B-A, B-S)
  8.4  essay components: SMD = raw difference / real-student SD; MCQ: difference in proportions, odds ratio (secondary)
  --   per-model contrasts within each arm when the design has several models
NOT automated: the section 8.5 random-effects meta-analysis across exams, I^2/Q, Holm-Bonferroni, moderators, the faculty-flag
rationale coding and the "AI passed" determination. They need many exams and human judgment; the per-exam outputs here feed
them (scoring-results.csv).

Real-student scores are never written out: only aggregates leave this module (FERPA posture).
Percentile is `(n_below + 0.5 * n_equal) / n_real`; the plan does not define it. VERIFY against Wave 1 convention.
"""
from __future__ import annotations

import csv
import io
import statistics
from dataclasses import dataclass
from pathlib import Path

from .design import Cell, Design


class ScoringError(Exception):
    pass


ID_CANDIDATES = ["examid", "exam id", "exam_id", "exam #", "exam#", "exam number", "examnumber", "id"]


def normalize_id(x: str) -> str:
    s = str(x).strip()
    if s.isdigit():
        return s.lstrip("0") or "0"
    return s


def _num(x) -> float | None:
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_scores(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    rdr = csv.DictReader(io.StringIO(text))
    if not rdr.fieldnames:
        raise ScoringError(f"{path}: no header row")
    return [h.strip() for h in rdr.fieldnames], [{k.strip(): (v or "") for k, v in row.items() if k} for row in rdr]


def find_column(headers: list[str], wanted: str | None, candidates: list[str] | None = None) -> str | None:
    low = {h.lower().strip(): h for h in headers}
    if wanted:
        if wanted.lower().strip() in low:
            return low[wanted.lower().strip()]
        raise ScoringError(f"column {wanted!r} not found; columns are {headers}")
    for c in candidates or []:
        if c in low:
            return low[c]
    return None


def class_stats(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "sd": None, "min": None, "max": None}
    return {"n": n, "mean": statistics.fmean(values), "median": statistics.median(values),
            "sd": statistics.stdev(values) if n > 1 else None,  # sample SD, n-1 divisor
            "min": min(values), "max": max(values)}


def standing(value: float, real: list[float], other_ai: list[float]) -> dict:
    st = class_stats(real)
    sd = st["sd"]
    below = sum(r < value for r in real)
    equal = sum(r == value for r in real)
    return {"raw": value,
            "z": (value - st["mean"]) / sd if sd else None,
            "rank_real_only": 1 + sum(r > value for r in real), "n_real": st["n"], "tied_with_real": equal > 0,
            "rank_combined": 1 + sum(r > value for r in real + other_ai), "n_combined": st["n"] + len(other_ai) + 1,
            "percentile": 100.0 * (below + 0.5 * equal) / st["n"] if st["n"] else None}


def mcq_scores(design: Design, store) -> dict[str, dict]:
    """Deterministic MCQ score per ExamID from the final (revised-else-capture) answers and the key."""
    from .package import parse_answer_key, parse_submission

    out: dict[str, dict] = {}
    for comp in design.mcqs():
        if not comp.answer_key_file or not design.path(comp.answer_key_file).is_file():
            continue
        key = parse_answer_key(design.path(comp.answer_key_file).read_text(encoding="utf-8"))
        for arm in design.arms:
            for model in design.models:
                cell = Cell(comp.id, arm.id, model.key)
                if not store.is_captured(cell):
                    continue
                ans = parse_submission(store.final_text(cell)[0])
                out[design.examid_for(arm.id, model.key)] = {
                    "component": comp.id, "correct": sum(ans.get(i) == k for i, k in key.items()), "n": len(key)}
    return out


def _odds_ratio(c1: int, n1: int, c2: int, n2: int) -> float | None:
    a, b, c, d = c1, n1 - c1, c2, n2 - c2
    if b == 0 or c == 0 or a == 0 or d == 0:
        return None  # undefined without a continuity correction, which the plan does not specify
    return (a / b) / (c / d)


def analyze(design: Design, headers: list[str], rows: list[dict[str, str]], *, id_column: str | None = None,
            total_column: str | None = None, component_columns: dict[str, str] | None = None,
            ai_flags: dict[str, str] | None = None, mcq_det: dict[str, dict] | None = None) -> dict:
    idc = find_column(headers, id_column, ID_CANDIDATES)
    if idc is None:
        raise ScoringError(f"no ExamID column found; pass --id-column (columns: {headers})")
    ai_map = {eid: (arm, mk) for (arm, mk), eid in design.examid_map().items()}  # keyed by the design's own spelling
    ai_norm = {normalize_id(e): e for e in ai_map}  # "0137" in the faculty file matches "137" and vice versa

    comp_cols: dict[str, str] = {}
    for c in design.components:
        col = (component_columns or {}).get(c.id) or find_column(headers, None, [c.id.lower()])
        if col:
            comp_cols[c.id] = find_column(headers, col)  # type: ignore[assignment]
    tot_col = find_column(headers, total_column, ["total", "total score", "total_score"] if not total_column else None)

    seen: set[str] = set()
    by_id: dict[str, dict[str, str]] = {}
    for r in rows:
        k = normalize_id(r.get(idc, ""))
        if not k:
            continue
        if k in seen:
            raise ScoringError(f"duplicate ExamID {k} in the scores file")
        seen.add(k)
        by_id[k] = r

    def row_metrics(r: dict[str, str]) -> dict[str, float | None]:
        m = {cid: _num(r.get(col)) for cid, col in comp_cols.items()}
        if tot_col:
            m["total"] = _num(r.get(tot_col))
        elif comp_cols and all(m[c] is not None for c in comp_cols):
            m["total"] = sum(m[c] for c in comp_cols)  # type: ignore[misc]
        else:
            m["total"] = None
        return m

    if not comp_cols and not tot_col:
        raise ScoringError(f"found neither a total column nor columns named for components {[c.id for c in design.components]}; "
                           f"pass --total-column / --component-column")

    missing_ai = sorted(e for e in ai_map if normalize_id(e) not in by_id)
    real_rows = {k: r for k, r in by_id.items() if k not in ai_norm}  # AI IDs excluded from every comparator
    ai_rows = {e: by_id[normalize_id(e)] for e in ai_map if normalize_id(e) in by_id}
    real_m = {k: row_metrics(r) for k, r in real_rows.items()}
    ai_m = {k: row_metrics(r) for k, r in ai_rows.items()}

    metrics = ["total"] + [c for c in comp_cols]
    kinds = {c.id: c.kind for c in design.components}
    result: dict = {"study_id": design.study_id, "id_column": idc, "components_found": comp_cols, "total_column": tot_col,
                    "n_rows": len(by_id), "n_real": len(real_rows), "ai_ids": {e: {"arm": a, "model_key": m} for e, (a, m) in ai_map.items()},
                    "missing_ai_ids": missing_ai, "metrics": {}, "arm_contrasts": [], "model_contrasts": [], "notes": []}
    if missing_ai:
        result["notes"].append(f"AI ExamIDs absent from the scores file: {missing_ai} (their rows are omitted)")
    ai_flags = {ai_norm[normalize_id(k)]: v for k, v in (ai_flags or {}).items() if normalize_id(k) in ai_norm}

    for metric in metrics:
        real_vals = [v[metric] for v in real_m.values() if v.get(metric) is not None]
        st = class_stats(real_vals)
        st["n_excluded_missing"] = len(real_m) - len(real_vals)
        ai_vals = {e: m.get(metric) for e, m in ai_m.items() if m.get(metric) is not None}
        entry = {"class": st, "ai": {}}
        for e, v in ai_vals.items():
            others = [x for o, x in ai_vals.items() if o != e]
            entry["ai"][e] = {**standing(v, real_vals, others), "arm": ai_map[e][0], "model_key": ai_map[e][1]}
        result["metrics"][metric] = entry

    for e, d in (mcq_det or {}).items():
        e = ai_norm.get(normalize_id(e))
        if e in ai_map and d["component"] not in comp_cols:
            met = result["metrics"].setdefault(d["component"], {"class": class_stats([]), "ai": {}})
            met["deterministic"] = True
            met["ai"][e] = {"raw": float(d["correct"]), "n_questions": d["n"], "arm": ai_map[e][0], "model_key": ai_map[e][1], "z": None,
                            "rank_real_only": None, "percentile": None}
            metrics = metrics if d["component"] in metrics else metrics + [d["component"]]
            kinds[d["component"]] = "mcq"

    arm_order = design.arm_ids
    model_keys = [m.key for m in design.models]

    def get(metric, arm, mk):
        eid = next((e for e, (a, m) in ai_map.items() if a == arm and m == mk), None)
        return (eid, result["metrics"].get(metric, {}).get("ai", {}).get(eid))

    for metric in metrics:
        sd = result["metrics"][metric]["class"]["sd"] if metric in result["metrics"] else None
        for mk in model_keys:
            for i, later in enumerate(arm_order):
                for earlier in arm_order[:i]:
                    _, x = get(metric, later, mk)
                    _, y = get(metric, earlier, mk)
                    if not x or not y:
                        continue
                    c = {"metric": metric, "model_key": mk, "later_arm": later, "earlier_arm": earlier,
                         "raw_diff": x["raw"] - y["raw"],
                         "z_diff": (x["z"] - y["z"]) if x.get("z") is not None and y.get("z") is not None else None,
                         "rank_diff": (x["rank_real_only"] - y["rank_real_only"]) if x.get("rank_real_only") and y.get("rank_real_only") else None}
                    if kinds.get(metric) == "essay":
                        c["smd"] = c["raw_diff"] / sd if sd else None
                    if kinds.get(metric) == "mcq":
                        n = x.get("n_questions") or design.component(metric).n_questions
                        c["prop_diff"] = (x["raw"] - y["raw"]) / n
                        c["odds_ratio"] = _odds_ratio(int(x["raw"]), n, int(y["raw"]), n)
                    result["arm_contrasts"].append(c)
        if len(model_keys) > 1:
            for arm in arm_order:
                for j, mb in enumerate(model_keys):
                    for ma in model_keys[:j]:
                        _, x = get(metric, arm, mb)
                        _, y = get(metric, arm, ma)
                        if not x or not y:
                            continue
                        result["model_contrasts"].append({
                            "metric": metric, "arm": arm, "model_b": mb, "model_a": ma, "raw_diff": x["raw"] - y["raw"],
                            "z_diff": (x["z"] - y["z"]) if x.get("z") is not None and y.get("z") is not None else None,
                            "rank_diff": (x["rank_real_only"] - y["rank_real_only"]) if x.get("rank_real_only") and y.get("rank_real_only") else None})
    result["faculty_ai_flag"] = {e: ai_flags.get(e, "unknown") for e in ai_map}
    return result


def csv_rows(design: Design, analysis: dict, *, notes: str = "") -> list[dict]:
    """One scoring-results.csv row per scored AI ExamID (columns documented in record.SCORING_COLUMNS)."""
    tot = analysis["metrics"].get("total", {"class": class_stats([]), "ai": {}})
    comp_ids = [m for m in analysis["metrics"] if m != "total"]
    out = []
    for e, info in analysis["ai_ids"].items():
        if e in analysis["missing_ai_ids"]:
            continue
        t = tot["ai"].get(e)
        comps, zs = [], []
        for cid in comp_ids:
            a = analysis["metrics"][cid]["ai"].get(e)
            if a is None:
                continue
            comps.append(f"{cid}={a['raw']:g}" + (f"/{a['n_questions']}" if a.get("n_questions") else ""))
            if a.get("z") is not None:
                zs.append(f"{cid}={a['z']:+.2f}")
        cs = tot["class"]
        method = "deterministic" if all(analysis["metrics"][c].get("deterministic") for c in comp_ids) and not t else "faculty-blind"
        out.append({
            "exam_date": design.exam.date or "", "course": design.exam.course, "professor": design.exam.professor,
            "examid": e, "arm": info["arm"], "model": design.model(info["model_key"]).model, "scoring_method": method,
            "component_scores": ";".join(comps), "component_z": ";".join(zs),
            "total_score": f"{t['raw']:g}" if t else "", "total_max": "",
            "class_rank": t["rank_real_only"] if t else "", "class_n_real": cs["n"] or "",
            "class_mean": f"{cs['mean']:.2f}" if cs["mean"] is not None else "",
            "class_median": f"{cs['median']:g}" if cs["median"] is not None else "",
            "class_sd": f"{cs['sd']:.2f}" if cs["sd"] is not None else "",
            "z_score": f"{t['z']:+.2f}" if t and t["z"] is not None else "",
            "percentile": f"{t['percentile']:.1f}" if t and t["percentile"] is not None else "",
            "faculty_ai_flag": analysis["faculty_ai_flag"].get(e, "unknown"), "notes": notes})
    return out
