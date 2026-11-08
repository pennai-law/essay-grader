"""Pipeline hook + CLI for citation checking (offline)."""
import asyncio
import json

from essay_grader import citations as C
from essay_grader.cli import main
from essay_grader.fake import FakeLLM, fake_decisions_transport
from essay_grader.graders.claude_grader import ClaudeGrader
from essay_grader.graders.openai_decisions_grader import OpenAIDecisionsGrader
from essay_grader.ingest import Answer, load_answers
from essay_grader.pipeline import GradeConfig, run_pipeline
from test_citations import DB, MEMO, make_client


def graders():
    return [ClaudeGrader(FakeLLM(), retry_delay=0), OpenAIDecisionsGrader(fake_decisions_transport, retry_delay=0)]


def run(locked, fix, exam_text, tmp_path, run_id, **kw):
    cfg = GradeConfig(run_id=run_id, out_dir=tmp_path, seed=7, **kw)
    d = asyncio.run(run_pipeline(locked, exam_text, load_answers(fix / "answers", locked), graders(), cfg))
    return {(r["student_id"], r["question_id"]): r for r in map(json.loads, (d / "results.jsonl").read_text().splitlines())}


def test_off_by_default(locked, fix, exam_text, tmp_path):
    recs = run(locked, fix, exam_text, tmp_path, "off")
    assert all("citations" not in r for r in recs.values())


def test_hook_adds_review_reason_but_never_changes_points(locked, fix, exam_text, tmp_path):
    base = run(locked, fix, exam_text, tmp_path, "base")
    client = make_client()
    seen = []
    target = (fix / "answers" / "S001.txt").read_text()

    def checker(text):
        seen.append(text)
        # only S001 "cites" a fabricated case (the checker is injected, so the answer text itself is irrelevant)
        return C.check(MEMO if text == target or target.strip() in text else "No authority cited here.", client=client)

    with_c = run(locked, fix, exam_text, tmp_path, "withc", check_citations=True, citation_checker=checker)
    assert len(seen) == len(base)
    flagged = [k for k, r in with_c.items() if "citation:fabricated_suspect" in r["flags"]]
    assert len(flagged) == 1
    for k, r in with_c.items():
        b = base[k]
        assert r["suggested_total"] == b["suggested_total"]  # points untouched
        assert r["criteria"] == b["criteria"]
        assert "citations" in r
        if k in flagged:
            assert r["status"] == "needs_human_review"
            assert r["citations"]["summary"]["fabricated_suspect"] == 1
        else:
            assert r["status"] == b["status"] and r["flags"] == b["flags"]
    queue = (tmp_path / "withc" / "review_queue.csv").read_text()
    assert "citation:fabricated_suspect" in queue


def test_checker_failure_does_not_lose_grades(locked, fix, exam_text, tmp_path):
    def boom(text):
        raise RuntimeError("network down")
    recs = run(locked, fix, exam_text, tmp_path, "boom", check_citations=True, citation_checker=boom)
    assert recs and all("citations" not in r and "network down" in r["citations_error"] for r in recs.values())


def test_cli_citations_check(tmp_path, capsys, monkeypatch):
    f = tmp_path / "memo.txt"
    f.write_text(MEMO)
    exp = tmp_path / "expected.yaml"
    exp.write_text("- Brown v. Board of Education\n- Marbury v. Madison\n")
    monkeypatch.delenv("COURTLISTENER_API_TOKEN", raising=False)
    out = tmp_path / "rep.md"
    assert main(["citations", "check", str(f), "--expected", str(exp), "--out", str(out), "--cache", ":memory:"]) == 0
    md = out.read_text()
    assert "347 U.S. 483" in md and "Missed (1): Marbury v. Madison" in md and "NOT checked" in md
    assert json.loads(out.with_suffix(".json").read_text())["verified"] is False
