"""Methodology record generator: formats, idempotence, em-dash discipline."""
import csv
import re
from datetime import date

from essay_grader.study.audit import audit_study
from essay_grader.study.materials import check_materials, sandbox_for
from essay_grader.study.record import SCORING_COLUMNS, SUMMARY_COLUMNS, write_run_records, write_scoring_records
from essay_grader.study.scoring import analyze, csv_rows, read_scores
from test_study_helpers import FIX, design, design_path, make_study, protocol, run, store  # noqa: F401

SYNC_SCRIPT_COLUMNS = ["exam_date", "course", "professor", "format", "delivery", "n_essays", "n_mcq", "essay_word_ceilings",
                       "arms_generated", "examids", "materials_provided_count", "materials_provided_summary", "generation_date",
                       "status", "notes", "scoring_status", "class_n_real", "a_total", "a_rank", "b_total", "b_rank", "c_total", "c_rank"]
DAY = date(2026, 12, 11)


def _record(design, store, protocol, **kw):
    run(design, store)
    bundles = {a.id: sandbox_for(design, a.id).load() for a in design.arms}
    audits = audit_study(design, store, protocol, citations=False)
    return write_run_records(design, store, protocol, bundles, audits=audits, today=DAY, naivety=check_materials(design), **kw)


def _all_md(store):
    return "\n".join(p.read_text() for p in (store.root / "methodology").glob("*.md"))


def test_summary_csv_has_exactly_the_sync_script_columns_and_one_row(design, store, protocol):
    _record(design, store, protocol)
    with open(store.root / "methodology/per-exam-summary.csv", newline="") as f:
        rdr = csv.DictReader(f)
        rows = list(rdr)
    assert rdr.fieldnames == SYNC_SCRIPT_COLUMNS == SUMMARY_COLUMNS  # sync_summary_md.py REQUIRED_COLUMNS, in order
    assert len(rows) == 1
    r = rows[0]
    assert (r["exam_date"], r["course"], r["professor"], r["format"], r["delivery"]) == ("2026-12-10", "Introductory Widget Law", "Quill", "MCQ + essay", "in-class")
    assert (r["n_essays"], r["n_mcq"], r["essay_word_ceilings"], r["arms_generated"]) == ("1", "5", "600", "A, S, B")
    assert r["examids"] == "A=0137; S=0460; B=0891" and r["materials_provided_count"] == "2" and "S: syllabus.md" in r["materials_provided_summary"]
    assert r["generation_date"] and r["scoring_status"] == "pending" and "prompt_version 2" in r["notes"]
    md = (store.root / "methodology/per-exam-summary.md").read_text()
    assert md.startswith("<!-- Generated from per-exam-summary.csv") and "| exam_date |" in md and "Introductory Widget Law" in md


def test_rerun_is_idempotent_and_updates_the_row_in_place(design, store, protocol):
    _record(design, store, protocol)
    snap = {p.name: p.read_text() for p in (store.root / "methodology").iterdir()}
    bundles = {a.id: sandbox_for(design, a.id).load() for a in design.arms}
    audits = audit_study(design, store, protocol, citations=False)
    write_run_records(design, store, protocol, bundles, audits=audits, today=DAY, naivety=check_materials(design))
    assert {p.name: p.read_text() for p in (store.root / "methodology").iterdir()} == snap  # no duplicate rows or entries
    rows = list(csv.DictReader(open(store.root / "methodology/per-exam-summary.csv")))
    assert len(rows) == 1


def test_materials_inventory_entry_in_the_exam_taker_schema(design, store, protocol):
    _record(design, store, protocol)
    t = (store.root / "methodology/materials-inventory.md").read_text()
    assert "## 2026-12-11: Introductory Widget Law (Quill)" in t
    for head in ("**Course Materials/ contents (operator reference, not exposed to model):**", "**Selection rationale:**", "**Gaps and notes:**"):
        assert head in t
    assert "Arm A: no materials" in t and "`outline.md`" in t and re.search(r"sha256 [0-9a-f]{12}", t) and "[HUMAN:" in t


def test_format_variations_lists_deltas_or_no_deltas(design, store, protocol, tmp_path):
    _record(design, store, protocol)
    t = (store.root / "methodology/format-variations.md").read_text()
    assert "essay-plus-MCQ" in t and "**Arms generated:** A, S, B (canonical: A, B)" in t and "No deltas." not in t

    from essay_grader.study.design import Design
    from essay_grader.study.store import StudyStore

    def canonical(d):  # the Wave 1 canonical shape: two essays, 1200/1000, A and B, one model
        d["components"] = [{"id": "q1", "kind": "essay", "question_file": "exam/q1.txt", "word_ceiling": 1200},
                           {"id": "q2", "kind": "essay", "question_file": "exam/q1.txt", "word_ceiling": 1000}]
        d["arms"] = [a for a in d["arms"] if a["id"] != "S"]
        d["examids"].pop("S")

    d2 = Design.load(make_study(tmp_path / "k", canonical))
    st2 = StudyStore(tmp_path / "k" / "out", d2)
    run(d2, st2)
    write_run_records(d2, st2, protocol, {a.id: sandbox_for(d2, a.id).load() for a in d2.arms}, today=DAY)
    assert "No deltas." in (st2.root / "methodology/format-variations.md").read_text()


def test_prompt_evolution_and_decision_log_stub(design, store, protocol):
    _record(design, store, protocol)
    pe = (store.root / "methodology/prompt-evolution.md").read_text()
    assert "v2" in pe and "**Change:**" in pe and "**Rationale:**" in pe and "**Triggered by:** lessons-learned L-043" in pe and "**Affected exams:**" in pe
    dl = (store.root / "methodology/decision-log.md").read_text()
    for head in ("**Context:**", "**Decision/event:**", "**Rationale:**", "**Protocol change (if any):**", "**Cross-references:**"):
        assert head in dl
    assert "6 of 6 cells captured verbatim" in dl and "[HUMAN:" in dl


def test_generated_records_contain_no_em_dashes(design, store, protocol):
    _record(design, store, protocol)
    headers, rows = read_scores(FIX / "scores.csv")
    a = analyze(design, headers, rows)
    write_scoring_records(design, store, a, csv_rows(design, a), today=DAY)
    assert "—" not in _all_md(store)
    for p in (store.root / "methodology").glob("*.csv"):
        assert "—" not in p.read_text()


def test_scoring_results_csv_rows_and_summary_columns_updated(design, store, protocol):
    _record(design, store, protocol)
    headers, rows = read_scores(FIX / "scores.csv")
    a = analyze(design, headers, rows)
    write_scoring_records(design, store, a, csv_rows(design, a), today=DAY)
    write_scoring_records(design, store, a, csv_rows(design, a), today=DAY)  # twice: rows are upserted, not duplicated
    sr = list(csv.DictReader(open(store.root / "methodology/scoring-results.csv")))
    assert [r["examid"] for r in sr] == ["0137", "0460", "0891"] and list(sr[0]) == SCORING_COLUMNS
    s = list(csv.DictReader(open(store.root / "methodology/per-exam-summary.csv")))[0]
    assert s["scoring_status"] == "scored" and s["class_n_real"] == "10"
    assert (s["a_total"], s["a_rank"], s["b_total"], s["b_rank"]) == ("100", "1", "100", "1")
    assert "S total=95 rank=1" in s["notes"]  # arm S has no column in the Wave 1 schema: kept in notes (see docs/STUDY.md)
    dl = (store.root / "methodology/decision-log.md").read_text()
    assert dl.count("Scoring return, Introductory Widget Law") == 1 and "real-student-only class statistics (n = 10" in dl


def test_existing_scoring_results_header_is_respected(design, store, protocol):
    d = store.d("methodology")
    (d / "scoring-results.csv").write_text("examid,arm,total_score,custom_col\n")
    headers, rows = read_scores(FIX / "scores.csv")
    a = analyze(design, headers, rows)
    write_scoring_records(design, store, a, csv_rows(design, a), today=DAY)
    got = list(csv.DictReader(open(d / "scoring-results.csv")))
    assert list(got[0]) == ["examid", "arm", "total_score", "custom_col"] and got[0]["total_score"] == "100" and got[0]["custom_col"] == ""
