"""`essay-grader study ...` end to end with fake providers and the fictional fixtures."""
import csv
import json
from pathlib import Path

import pytest

from essay_grader.cli import build_parser, main
from test_study_helpers import FIX, make_study


def _cli(*argv):
    return main(["study", *map(str, argv)])


@pytest.fixture
def d(tmp_path):
    return make_study(tmp_path)


def test_subcommands_are_registered():
    sub = [a for a in build_parser()._subparsers._group_actions[0].choices["study"]._subparsers._group_actions[0].choices]
    assert sub == ["validate", "check-materials", "run", "audit", "revise", "package", "ingest-scores", "report"]


def test_validate_ok_and_error_exit_codes(d, tmp_path, capsys):
    assert _cli("validate", d) == 0 and "6 cells" in capsys.readouterr().out
    bad = make_study(tmp_path / "bad", lambda x: x["examids"].update(S="0137"))
    assert _cli("validate", bad) == 2 and "ERROR" in capsys.readouterr().out
    assert _cli("validate", tmp_path / "missing.yaml") == 2


def test_check_materials_exit_code(d, tmp_path, capsys):
    assert _cli("check-materials", d) == 0
    (d.parent / "materials/full/answer_key.txt").write_text("Q1: A")
    assert _cli("check-materials", d) == 7 and "BLOCK" in capsys.readouterr().out


def test_full_dry_run_workflow(d, tmp_path, capsys):
    out = tmp_path / "o"
    assert _cli("run", d, "--dry-run", "--out", out, "--budget-usd", 5) == 0
    root = out / "dryrun" / "fictional-fall-2026-widgets"
    assert len(list((root / "captures").glob("*.md"))) == 6 and (root / "methodology/per-exam-summary.csv").is_file()
    capsys.readouterr()
    assert _cli("run", d, "--dry-run", "--out", out) == 0  # resume: nothing left to do
    assert "Captured 0, resumed/skipped 6" in capsys.readouterr().out
    assert _cli("audit", d, "--dry-run", "--out", out, "--strict") == 0
    assert _cli("revise", d, "--dry-run", "--out", out) == 0
    assert "Verbatim captures were not modified" in capsys.readouterr().out
    assert _cli("package", d, "--dry-run", "--out", out) == 0
    assert list((root / "package/deliver").glob("MC_Results_*.xlsx"))
    assert _cli("ingest-scores", d, "--dry-run", "--out", out, "--scores", FIX / "scores.csv", "--ai-flag", "0137=yes") == 0
    rows = list(csv.DictReader(open(root / "methodology/scoring-results.csv")))
    assert [r["examid"] for r in rows] == ["0137", "0460", "0891"] and rows[0]["faculty_ai_flag"] == "yes"
    assert json.loads((root / "scoring/scoring-analysis.json").read_text())["n_real"] == 10
    assert _cli("report", d, "--dry-run", "--out", out) == 0
    assert "Arm contrasts" in (root / "report.md").read_text() and (root / "methodology/cohort-results.draft.md").is_file()


def test_run_is_blocked_by_naivety_with_exit_7_and_no_captures(d, tmp_path, capsys):
    (d.parent / "materials/syllabus/notes.md").write_text("The essay must not exceed 600 words.")
    assert _cli("run", d, "--dry-run", "--out", tmp_path / "o") == 7
    err = capsys.readouterr().err
    assert "word_limit" in err and "run will not start" in err
    assert not list((tmp_path / "o").rglob("*.md"))


def test_budget_stop_exit_code_and_resume_message(d, tmp_path, capsys):
    assert _cli("run", d, "--dry-run", "--out", tmp_path / "o", "--budget-usd", 0.0) == 5
    assert "STOPPED: budget exhausted" in capsys.readouterr().out


def test_protocol_drift_exit_code(d, tmp_path, capsys):
    t = d.parent / "protocol/prompt_template_essay.txt"
    t.write_text(t.read_text() + "\nedited\n")
    assert _cli("run", d, "--dry-run", "--out", tmp_path / "o") == 2  # validate refuses first (protocol error listed)
    assert "protocol:" in capsys.readouterr().err
    assert _cli("audit", d, "--dry-run", "--out", tmp_path / "o") == 8
    assert "protocol drift" in capsys.readouterr().err


def test_package_refuses_unrevised_violations_with_exit_9(d, tmp_path, capsys):
    out = tmp_path / "o"
    assert _cli("run", d, "--dry-run", "--out", out) == 0
    cap = next((out / "dryrun").rglob("q1__B__sonnet.md"))
    cap.chmod(0o644)
    text = cap.read_text()
    cap.write_text(text)  # unchanged content so the recorded hash still matches
    cap.chmod(0o444)
    meta = cap.with_name("q1__B__sonnet.meta.json")
    m = json.loads(meta.read_text())
    import hashlib

    dirty = "Notably, " + text
    cap.chmod(0o644)
    cap.write_text(dirty)
    cap.chmod(0o444)
    m["capture_sha256"] = hashlib.sha256(dirty.encode()).hexdigest()
    meta.write_text(json.dumps(m))
    assert _cli("package", d, "--dry-run", "--out", out) == 9
    assert "fails the audit" in capsys.readouterr().err


def test_ingest_scores_error_exit_code(d, tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("name,foo\nx,1\n")
    assert _cli("ingest-scores", d, "--dry-run", "--out", tmp_path / "o", "--scores", bad) == 2
    assert "ExamID column" in capsys.readouterr().err
