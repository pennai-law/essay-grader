import asyncio
import csv
import json
from pathlib import Path

import pytest

from essay_grader.cli import main
from essay_grader.fake import FakeLLM, fake_decisions_transport
from essay_grader.graders.claude_grader import ClaudeGrader
from essay_grader.graders.openai_decisions_grader import OpenAIDecisionsGrader
from essay_grader.ingest import load_answers
from essay_grader.pipeline import GradeConfig, run_pipeline
from essay_grader.rubric import RubricNotLocked


def graders(retry=0):
    return [ClaudeGrader(FakeLLM(), retry_delay=retry), OpenAIDecisionsGrader(fake_decisions_transport, retry_delay=retry)]


def run(locked, fix, exam_text, tmp_path, gs=None, **kw):
    cfg = GradeConfig(run_id="r1", out_dir=tmp_path, seed=7, **kw)
    return asyncio.run(run_pipeline(locked, exam_text, load_answers(fix / "answers", locked), gs or graders(), cfg))


def test_end_to_end_outputs(locked, fix, exam_text, tmp_path):
    d = run(locked, fix, exam_text, tmp_path)
    for f in ["results.jsonl", "scores.csv", "review_queue.csv", "agreement_report.md", "run_metadata.json"]:
        assert (d / f).exists()
    recs = [json.loads(l) for l in (d / "results.jsonl").read_text().splitlines()]
    assert {r["student_id"] for r in recs} == {"S001", "S002", "S003"}
    s3 = next(r for r in recs if r["student_id"] == "S003")
    assert s3["truncated"] and s3["words"] == 143 and s3["words_graded"] == 120
    rows = list(csv.DictReader((d / "scores.csv").open()))
    assert len(rows) == 3 and "Q1.ucc_applies.claude" in rows[0] and "Q1.ucc_applies.openai" in rows[0]
    meta = json.loads((d / "run_metadata.json").read_text())
    assert meta["rubric"]["hash"] == locked.content_hash and meta["seed"] == 7
    assert {g["name"] for g in meta["graders"]} == {"claude", "openai"} and meta["usage"]
    for r in recs:  # statuses are suggestions only
        assert r["status"] in {"agreed", "needs_human_review"}
        if r["status"] == "needs_human_review":
            assert r["suggested_total"] is None
    queue = list(csv.DictReader((d / "review_queue.csv").open()))
    assert len(queue) == sum(r["status"] == "needs_human_review" for r in recs)


def test_blinding_graders_get_no_student_id(locked, fix, exam_text, tmp_path):
    seen = []

    class Spy(FakeLLM):
        async def call(self, **kw):
            seen.append(kw["prefix"] + kw["user"])
            return await super().call(**kw)

    run(locked, fix, exam_text, tmp_path, gs=[ClaudeGrader(Spy(), retry_delay=0)])
    assert seen and not any("S001" in s or "S002" in s for s in seen)


def test_refuses_unlocked_and_tampered(draft, locked, fix, exam_text, tmp_path):
    with pytest.raises(RubricNotLocked):
        run(draft, fix, exam_text, tmp_path)
    locked.questions[0].criteria[0].max_points = 9
    with pytest.raises(RubricNotLocked):
        run(locked, fix, exam_text, tmp_path)


def test_order_is_seeded(locked, fix, exam_text, tmp_path):
    d1 = run(locked, fix, exam_text, tmp_path / "a")
    d2 = run(locked, fix, exam_text, tmp_path / "b")
    order = lambda d: [(json.loads(l)["student_id"], json.loads(l)["order_index"]) for l in (d / "results.jsonl").read_text().splitlines()]
    assert sorted(order(d1), key=lambda x: x[1]) == sorted(order(d2), key=lambda x: x[1])


def test_resume_is_idempotent_and_retries_errors(locked, fix, exam_text, tmp_path):
    class Flaky(FakeLLM):
        fail = True

        async def call(self, **kw):
            if Flaky.fail and "S002" not in kw["user"] and "no contract" in kw["user"]:
                raise RuntimeError("API down")
            return await super().call(**kw)

    gs = [ClaudeGrader(Flaky(), retry_delay=0), OpenAIDecisionsGrader(fake_decisions_transport, retry_delay=0)]
    d = run(locked, fix, exam_text, tmp_path, gs=gs)
    lines1 = (d / "results.jsonl").read_text().splitlines()
    assert len(lines1) == 3
    bad = [json.loads(l) for l in lines1 if "grader_error:claude" in json.loads(l)["flags"]]
    assert len(bad) == 1 and bad[0]["status"] == "needs_human_review"
    Flaky.fail = False
    d = run(locked, fix, exam_text, tmp_path, gs=gs)  # only the failed answer is re-graded
    lines2 = (d / "results.jsonl").read_text().splitlines()
    assert len(lines2) == 4 and lines2[:3] == lines1
    d = run(locked, fix, exam_text, tmp_path, gs=gs)  # nothing pending now
    assert (d / "results.jsonl").read_text().splitlines() == lines2
    rows = list(csv.DictReader((d / "scores.csv").open()))
    assert len(rows) == 3
    assert all("grader_error" not in r["flags"] for r in rows)


def test_resume_refuses_changed_config(locked, fix, exam_text, tmp_path):
    run(locked, fix, exam_text, tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(run_pipeline(locked, exam_text, load_answers(fix / "answers", locked), graders(),
                                 GradeConfig(run_id="r1", out_dir=tmp_path, seed=8)))


def test_calibration_samples_flagged_or_excluded(locked, fix, exam_text, tmp_path):
    from essay_grader.rubric import Calibration

    cal = locked.with_rulings([], calibration=Calibration(sample_ids=["S002"])).lock()
    d = run(cal, fix, exam_text, tmp_path / "f")
    rows = {r["student_id"]: r for r in csv.DictReader((d / "scores.csv").open())}
    assert rows["S002"]["calibration_sample"] == "True" and rows["S001"]["calibration_sample"] == "False"
    d = run(cal, fix, exam_text, tmp_path / "x", exclude_calibration=True)
    assert {r["student_id"] for r in csv.DictReader((d / "scores.csv").open())} == {"S001", "S003"}


def test_cli_lock_and_dry_run(fix, tmp_path, monkeypatch, capsys):
    import shutil

    shutil.copy(fix / "rubric.yaml", tmp_path / "rubric.yaml")
    assert main(["lock", str(tmp_path / "rubric.yaml")]) == 0
    locked = tmp_path / "rubric.locked.yaml"
    assert locked.exists()
    base = ["--rubric", str(locked), "--exam", str(fix / "exam.txt")]
    assert main(["grade", *base, "--answers", str(fix / "answers"), "--dry-run", "--out", str(tmp_path / "runs"), "--run-id", "t"]) == 0
    assert (tmp_path / "runs" / "t" / "scores.csv").exists()
    # unlocked rubric is refused by the CLI
    assert main(["grade", "--rubric", str(tmp_path / "rubric.yaml"), "--exam", str(fix / "exam.txt"),
                 "--answers", str(fix / "answers"), "--dry-run", "--out", str(tmp_path / "runs2")]) == 3
    # calibrate -> new unlocked version -> must be re-locked
    assert main(["calibrate", *base, "--sample", str(fix / "answers"), "--dry-run", "--out", str(tmp_path / "cal")]) == 0
    v2 = tmp_path / "cal" / "rubric.v2.yaml"
    assert v2.exists()
    assert main(["grade", "--rubric", str(v2), "--exam", str(fix / "exam.txt"), "--answers", str(fix / "answers"),
                 "--dry-run", "--out", str(tmp_path / "runs3")]) == 3
    assert main(["harden", "--exam", str(fix / "exam.txt"), "--syllabus", str(fix / "exam.txt"),
                 "--rubric", str(tmp_path / "rubric.yaml"), "--dry-run", "--out", str(tmp_path / "h"), "--n", "4", "--k", "2"]) == 0
    assert list((tmp_path / "h").glob("harden-*/harden_report.md"))
