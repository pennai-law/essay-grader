"""Scoring intake and analysis: real-student-only stats (sample SD, n-1), AI z/rank, arm and model contrasts."""
import csv
import json
import statistics

import pytest

from essay_grader.study.design import Design
from essay_grader.study.scoring import (ScoringError, analyze, class_stats, csv_rows, mcq_scores, normalize_id, read_scores, standing)
from essay_grader.study.store import StudyStore
from test_study_helpers import FIX, clean_script, design, design_path, make_study, multi_model, run, store  # noqa: F401

REAL = [50, 55, 60, 60, 65, 70, 75, 80, 85, 90]
MEAN = 69.0
SD = statistics.stdev(REAL)  # 13.2916..., n-1 divisor


def _scores(design):
    return read_scores(FIX / "scores.csv")


def test_sample_sd_uses_n_minus_one():
    st = class_stats([float(x) for x in REAL])
    assert st["n"] == 10 and st["mean"] == MEAN and st["median"] == 67.5
    assert st["sd"] == pytest.approx((1590 / 9) ** 0.5) == pytest.approx(13.29160, abs=1e-5)
    assert st["sd"] != pytest.approx((1590 / 10) ** 0.5)  # NOT the population SD
    assert class_stats([5.0])["sd"] is None and class_stats([])["n"] == 0


def test_ai_rows_are_excluded_from_the_comparator_distribution(design):
    headers, rows = _scores(design)
    a = analyze(design, headers, rows)
    tot = a["metrics"]["total"]
    assert tot["class"]["n"] == 10 and tot["class"]["mean"] == MEAN and tot["class"]["sd"] == pytest.approx(SD)
    # the file has 13 rows, three of them AI (scores 95-100): including them would shift the mean to ~76 and the SD
    assert a["n_rows"] == 13 and a["n_real"] == 10
    everyone = [float(r["Total"]) for r in rows]
    assert statistics.fmean(everyone) != pytest.approx(MEAN) and statistics.stdev(everyone) != pytest.approx(SD)
    # AI z-scores are against the real-only mean and SD
    z = {e: v["z"] for e, v in tot["ai"].items()}
    assert z["0137"] == pytest.approx((100 - MEAN) / SD) and z["0460"] == pytest.approx((95 - MEAN) / SD)


def test_rank_percentile_ties_and_combined_rank():
    real = [50.0, 60.0, 60.0, 70.0, 80.0]
    s = standing(60.0, real, other_ai=[90.0])
    assert s["rank_real_only"] == 3 and s["tied_with_real"] and s["n_real"] == 5  # ties share the better rank
    assert s["rank_combined"] == 4  # the other AI cell outranks it in the combined list
    assert s["percentile"] == pytest.approx(100 * (1 + 0.5 * 2) / 5)
    top = standing(100.0, real, [])
    assert top["rank_real_only"] == 1 and top["z"] is not None and standing(1.0, [5.0], [])["z"] is None


def test_exam_ids_match_with_or_without_leading_zeros(design):
    assert normalize_id(" 0137 ") == "137" and normalize_id("0000") == "0" and normalize_id("abc") == "abc"
    headers, rows = _scores(design)
    for r in rows:
        r["ExamID"] = r["ExamID"].lstrip("0")  # faculty file without zero padding
    a = analyze(design, headers, rows)
    assert set(a["metrics"]["total"]["ai"]) == {"0137", "0460", "0891"} and a["n_real"] == 10  # display ids follow the design


def test_arm_contrasts_a_vs_s_vs_b(design):
    headers, rows = _scores(design)
    rows = [{**r, "Total": {"0137": "80", "0460": "90", "0891": "70"}.get(r["ExamID"], r["Total"]), "q1": {"0137": "80", "0460": "90", "0891": "70"}.get(r["ExamID"], r["q1"])} for r in rows]
    a = analyze(design, headers, rows)
    c = {(x["later_arm"], x["earlier_arm"]): x for x in a["arm_contrasts"] if x["metric"] == "total"}
    assert set(c) == {("S", "A"), ("B", "A"), ("B", "S")}
    assert c[("S", "A")]["raw_diff"] == 10 and c[("B", "A")]["raw_diff"] == -10 and c[("B", "S")]["raw_diff"] == -20
    assert c[("S", "A")]["z_diff"] == pytest.approx(10 / SD) and c[("B", "S")]["z_diff"] == pytest.approx(-20 / SD)
    # ranks vs real students only: A=80 -> 3rd (85, 90 above), S=90 -> tied for 1st, B=70 -> 5th (75, 80, 85, 90 above; ties share it)
    ranks = {v["arm"]: v["rank_real_only"] for v in a["metrics"]["total"]["ai"].values()}
    assert ranks == {"A": 3, "S": 1, "B": 5}
    assert c[("S", "A")]["rank_diff"] == ranks["S"] - ranks["A"] and c[("S", "A")]["rank_diff"] < 0  # negative = later arm ranks better
    q = {(x["later_arm"], x["earlier_arm"]): x for x in a["arm_contrasts"] if x["metric"] == "q1"}
    assert q[("S", "A")]["smd"] == pytest.approx(10 / SD)  # essay components get an SMD


def test_deterministic_mcq_contrast_has_proportion_difference_and_odds_ratio(design, store):
    run(design, store, providers={"anthropic": __import__("essay_grader.study.providers", fromlist=["x"]).FakeProvider(
        "anthropic", script=lambda r: {"mcq__A__sonnet": "1. B\n2. A\n3. A\n4. C\n5. C\n", "mcq__S__sonnet": "1. B\n2. B\n3. B\n4. A\n5. C\n",
                                       "mcq__B__sonnet": "1. B\n2. B\n3. B\n4. A\n5. A\n"}.get(r.cell_id) or clean_script(r))})
    det = mcq_scores(design, store)
    assert {e: d["correct"] for e, d in det.items()} == {"0137": 1, "0460": 4, "0891": 5}
    headers, rows = _scores(design)
    a = analyze(design, headers, rows, mcq_det=det)
    m = a["metrics"]["mcq"]
    assert m["deterministic"] and m["ai"]["0891"]["raw"] == 5 and m["ai"]["0891"]["z"] is None  # no real-student MCQ data supplied
    c = {(x["later_arm"], x["earlier_arm"]): x for x in a["arm_contrasts"] if x["metric"] == "mcq"}
    assert c[("S", "A")]["prop_diff"] == pytest.approx(3 / 5)
    assert c[("S", "A")]["odds_ratio"] == pytest.approx((4 / 1) / (1 / 4))  # (4/1)/(1/4) = 16
    assert c[("B", "S")]["odds_ratio"] is None  # a zero cell: undefined, and the plan specifies no correction


def test_faculty_ai_flags_and_missing_ai_rows(design):
    headers, rows = _scores(design)
    rows = [r for r in rows if r["ExamID"] != "0460"]  # an AI row absent from the registrar workbook
    a = analyze(design, headers, rows, ai_flags={"0137": "yes", "891": "no"})
    assert a["missing_ai_ids"] == ["0460"] and any("absent" in n for n in a["notes"])
    assert a["faculty_ai_flag"] == {"0137": "yes", "0460": "unknown", "0891": "no"}
    assert all(c["later_arm"] != "S" and c["earlier_arm"] != "S" for c in a["arm_contrasts"])  # no S row, no S contrast


def test_blank_and_nonnumeric_real_scores_are_excluded_and_counted(design):
    headers, rows = _scores(design)
    rows[0]["Total"], rows[0]["q1"] = "", ""
    rows[1]["Total"], rows[1]["q1"] = "ABS", "ABS"
    a = analyze(design, headers, rows)
    assert a["metrics"]["total"]["class"]["n"] == 8 and a["metrics"]["total"]["class"]["n_excluded_missing"] == 2


def test_errors_for_duplicate_ids_and_missing_columns(design):
    headers, rows = _scores(design)
    with pytest.raises(ScoringError, match="duplicate"):
        analyze(design, headers, rows + [dict(rows[0])])
    with pytest.raises(ScoringError, match="ExamID column"):
        analyze(design, ["name", "q1"], [{"name": "x", "q1": "1"}])
    with pytest.raises(ScoringError, match="neither a total"):
        analyze(design, ["ExamID", "foo"], [{"ExamID": "1", "foo": "2"}])
    with pytest.raises(ScoringError, match="not found"):
        analyze(design, headers, rows, total_column="Nope")


def test_total_is_summed_from_components_when_no_total_column(design):
    headers, rows = _scores(design)
    h2 = [h for h in headers if h != "Total"]
    a = analyze(design, h2, [{k: v for k, v in r.items() if k != "Total"} for r in rows])
    assert a["metrics"]["total"]["class"]["mean"] == MEAN


def test_per_model_comparison_when_there_are_several_models(tmp_path):
    d = Design.load(make_study(tmp_path, multi_model))
    ids = d.examid_map()
    headers, rows = ["ExamID", "q1", "Total"], [{"ExamID": "%04d" % (i + 1), "q1": str(v), "Total": str(v)} for i, v in enumerate(REAL)]
    for (arm, mk), eid in ids.items():
        score = {"sonnet": 90, "gpt": 70}[mk] + {"A": 0, "S": 5, "B": 10}[arm]
        rows.append({"ExamID": eid, "q1": str(score), "Total": str(score)})
    a = analyze(d, headers, rows)
    mc = [x for x in a["model_contrasts"] if x["metric"] == "total"]
    assert len(mc) == 3 and all(x["model_b"] == "gpt" and x["model_a"] == "sonnet" and x["raw_diff"] == -20 for x in mc)
    assert len([x for x in a["arm_contrasts"] if x["metric"] == "total"]) == 3 * 2  # per model
    assert {x["model_key"] for x in a["arm_contrasts"]} == {"sonnet", "gpt"}
    assert csv_rows(d, a)[0]["model"] in ("claude-sonnet-5-5", "gpt-6-luna") and len(csv_rows(d, a)) == 6


def test_scoring_results_rows_follow_the_exam_taker_columns(design):
    headers, rows = _scores(design)
    out = csv_rows(design, analyze(design, headers, rows))
    r = next(x for x in out if x["examid"] == "0137")
    assert r["arm"] == "A" and r["total_score"] == "100" and r["class_n_real"] == 10 and r["class_mean"] == "69.00"
    assert r["class_sd"] == "13.29" and r["z_score"] == "+2.33" and r["class_rank"] == 1 and r["scoring_method"] == "faculty-blind"
    assert r["course"] == "Introductory Widget Law" and r["faculty_ai_flag"] == "unknown"


def test_no_real_student_scores_leave_the_analysis(design, tmp_path):
    headers, rows = _scores(design)
    a = analyze(design, headers, rows)
    blob = json.dumps(a)
    for real_id in ("0001", "0002", "0010"):
        assert f'"{real_id}"' not in blob
    assert "ABS" not in blob and set(a["metrics"]["total"]["ai"]) == {"0137", "0460", "0891"}
