"""Command-line interface: harden | lock | calibrate | grade."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .agreement import AgreementConfig
from .ingest import extract_text, load_answers
from .llm import DEFAULT_CLAUDE_MODEL
from .rubric import RubricNotLocked, load_rubric, locked_path_for, save_rubric


def _claude(args) -> "object":
    if args.dry_run:
        from .fake import FakeLLM

        return FakeLLM()
    from .llm import AnthropicLLM

    return AnthropicLLM(args.model)


def cmd_lock(args) -> int:
    r = load_rubric(args.rubric)
    out = Path(args.out) if args.out else locked_path_for(args.rubric)
    locked = r.lock()
    save_rubric(locked, out)
    print(f"Locked {r.exam_id} v{r.version} -> {out}\nsha256: {locked.content_hash}")
    return 0


def cmd_harden(args) -> int:
    from .harden import HardenConfig, harden, write_report

    rubric = load_rubric(args.rubric)
    rep = asyncio.run(harden(rubric, extract_text(args.exam), extract_text(args.syllabus), _claude(args),
                             HardenConfig(n_synthetic=args.n, k=args.k, expected_words=args.expected_words)))
    d = write_report(rep, args.out)
    print(f"Hardening report: {d / 'harden_report.md'} ({len(rep['findings'])} findings)")
    return 0


def cmd_calibrate(args) -> int:
    from .calibrate import calibrate, write_calibration

    rubric = load_rubric(args.rubric)
    samples = load_answers(args.sample, rubric)
    new, rep = asyncio.run(calibrate(rubric, extract_text(args.exam), samples, _claude(args)))
    rpath, cpath = write_calibration(new, rep, Path(args.rubric), Path(args.out))
    print(f"New rubric v{new.version} (UNLOCKED): {rpath}\nReport: {cpath}\nReview, then `essay-grader lock {rpath}`.")
    print(f"Calibration sample ids recorded in rubric: {len(new.calibration.sample_ids)}")
    return 0


def cmd_grade(args) -> int:
    from .pipeline import GradeConfig, run_pipeline

    rubric = load_rubric(args.rubric)
    rubric.require_locked()
    names = [n.strip() for n in args.graders.split(",") if n.strip()]
    graders = []
    if "claude" in names:
        from .graders.claude_grader import ClaudeGrader

        graders.append(ClaudeGrader(_claude(args)))
    if "openai" in names:
        from .graders.openai_decisions_grader import OpenAIDecisionsGrader

        if args.dry_run:
            from .fake import fake_decisions_transport

            graders.append(OpenAIDecisionsGrader(fake_decisions_transport, threshold=args.decision_threshold))
        else:
            graders.append(OpenAIDecisionsGrader(model=args.openai_model, threshold=args.decision_threshold))
    unknown = set(names) - {"claude", "openai"}
    if unknown or not graders:
        print(f"unknown/empty --graders: {names}", file=sys.stderr)
        return 2
    answers = load_answers(args.answers, rubric)
    for a in answers:
        for w in a.warnings:
            print(f"WARNING {a.source}: {w}", file=sys.stderr)
    cfg = GradeConfig(out_dir=Path(args.out), seed=args.seed, concurrency=args.concurrency,
                      exclude_calibration=args.exclude_calibration, dry_run=args.dry_run,
                      agreement=AgreementConfig(args.disagreement_threshold, args.low_confidence, args.combine))
    if args.run_id:
        cfg.run_id = args.run_id
    elif args.dry_run:
        cfg.run_id = "dryrun-" + cfg.run_id
    d = asyncio.run(run_pipeline(rubric, extract_text(args.exam), answers, graders, cfg))
    print(f"Run complete: {d}\nSuggestions only; see review_queue.csv and agreement_report.md.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="essay-grader", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, exam=True):
        if exam:
            sp.add_argument("--exam", required=True, help="exam question file (pdf/docx/txt)")
        sp.add_argument("--model", default=DEFAULT_CLAUDE_MODEL)
        sp.add_argument("--dry-run", action="store_true", help="offline fake clients; no API calls")

    h = sub.add_parser("harden", help="stress-test a DRAFT rubric before any student exam is seen")
    common(h)
    h.add_argument("--syllabus", required=True)
    h.add_argument("--rubric", required=True)
    h.add_argument("--out", default="runs")
    h.add_argument("--n", type=int, default=11, help="synthetic answers per question")
    h.add_argument("--k", type=int, default=3, help="grading repeats per synthetic answer")
    h.add_argument("--expected-words", type=int, default=800)
    h.set_defaults(func=cmd_harden)

    l = sub.add_parser("lock", help="freeze a rubric with a sha256 content hash")
    l.add_argument("rubric")
    l.add_argument("--out")
    l.set_defaults(func=cmd_lock)

    c = sub.add_parser("calibrate", help="propose rulings from a small anonymized sample (creates a new rubric version)")
    common(c)
    c.add_argument("--rubric", required=True)
    c.add_argument("--sample", required=True)
    c.add_argument("--out", default="runs/calibration")
    c.set_defaults(func=cmd_calibrate)

    g = sub.add_parser("grade", help="dual-grade answers with a LOCKED rubric")
    common(g)
    g.add_argument("--rubric", required=True)
    g.add_argument("--answers", required=True)
    g.add_argument("--graders", default="claude,openai")
    g.add_argument("--openai-model", default="gpt-6-luna")
    g.add_argument("--out", default="runs")
    g.add_argument("--run-id")
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--concurrency", type=int, default=4)
    g.add_argument("--disagreement-threshold", type=float, default=1.0)
    g.add_argument("--low-confidence", type=float, default=0.5)
    g.add_argument("--decision-threshold", type=float, default=0.5)
    g.add_argument("--combine", choices=["mean", "min", "max"], default="mean")
    g.add_argument("--exclude-calibration", action="store_true")
    g.set_defaults(func=cmd_grade)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RubricNotLocked as e:
        print(f"error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
