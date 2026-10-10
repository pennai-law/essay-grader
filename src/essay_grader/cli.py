"""Command-line interface: harden | apply-answers | lock | calibrate | grade."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

from .agreement import AgreementConfig
from .budget import Budget, BudgetExceeded
from .ingest import extract_text, load_answers
from .llm import DEFAULT_CLAUDE_MODEL
from .rubric import RubricNotLocked, load_rubric, locked_path_for, save_rubric


def _claude(args) -> "object":
    if args.dry_run:
        from .fake import FakeLLM

        return FakeLLM()
    from .llm import AnthropicLLM

    return AnthropicLLM(args.model)


def _budget(args) -> Budget:
    usd = getattr(args, "budget_usd", None)
    return Budget(max_usd=usd) if usd is not None else Budget.unlimited()


def cmd_lock(args) -> int:
    r = load_rubric(args.rubric)
    out = Path(args.out) if args.out else locked_path_for(args.rubric)
    locked = r.lock()
    save_rubric(locked, out)
    print(f"Locked {r.exam_id} v{r.version} -> {out}\nsha256: {locked.content_hash}")
    return 0


def _decisions(args, *, allow_draft: bool = False):
    from .graders.openai_decisions_grader import OpenAIDecisionsGrader

    if args.dry_run:
        from .fake import fake_decisions_transport

        return OpenAIDecisionsGrader(fake_decisions_transport, threshold=args.decision_threshold, allow_draft=allow_draft)
    return OpenAIDecisionsGrader(model=args.openai_model, threshold=args.decision_threshold, allow_draft=allow_draft)


def cmd_harden(args) -> int:
    from .harden import HardenConfig, harden, write_report

    rubric = load_rubric(args.rubric)
    budget = _budget(args)
    use_dec = not args.no_decisions
    rep = asyncio.run(harden(rubric, extract_text(args.exam), extract_text(args.syllabus), _claude(args),
                             decisions=_decisions(args, allow_draft=True) if use_dec else None, budget=budget,
                             cfg=HardenConfig(n_archetypes=args.n, k=args.k, expected_words=args.expected_words,
                                              max_questions=args.max_questions, use_decisions=use_dec,
                                              critique_model=args.model, questionnaire_model=args.model)))
    d = write_report(rep, args.out)
    print(f"Questionnaire: {d / 'questionnaire.md'} ({len(rep['questionnaire']['questions'])} questions; "
          f"also {d / 'questionnaire.json'})\nReport: {d / 'harden_report.md'}\n"
          f"Spent ${rep['budget']['spent_usd']:.2f}; {len(rep['degradations'])} degradation(s) recorded.\n"
          f"Answer ALL questions once in answers.yaml, then `essay-grader apply-answers`, then `essay-grader lock`.")
    return 0


def cmd_apply_answers(args) -> int:
    from .questionnaire import Questionnaire, apply_answers

    rubric = load_rubric(args.rubric)
    qn = Questionnaire.model_validate_json(Path(args.questionnaire).read_text(encoding="utf-8"))
    answers = yaml.safe_load(Path(args.answers).read_text(encoding="utf-8")) or {}
    if not isinstance(answers, dict):
        print("error: answers file must be a mapping of question id -> option label or custom text", file=sys.stderr)
        return 2
    try:
        new = apply_answers(rubric, qn, {str(k): v for k, v in answers.items()}, allow_stale=args.allow_stale)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else Path(args.rubric).with_name(f"{Path(args.rubric).name.split('.')[0]}.v{new.version}.yaml")
    save_rubric(new, out)
    n_def = sum(p.source == "recommended_default" and p.run_kind == qn.run_kind and p.hardening_run_id == qn.run_id
                for p in new.provenance)
    print(f"New draft v{new.version} -> {out}\n{len(qn.questions)} question(s) applied ({n_def} defaulted to the recommended option).\n"
          f"Next: `essay-grader lock {out}`.")
    return 0


def cmd_calibrate(args) -> int:
    from .calibrate import calibrate, write_calibration

    rubric = load_rubric(args.rubric)
    samples = load_answers(args.sample, rubric)
    new, rep = asyncio.run(calibrate(rubric, extract_text(args.exam), samples, _claude(args), budget=_budget(args),
                                     max_questions=args.max_questions))
    rpath, cpath = write_calibration(new, rep, Path(args.rubric), Path(args.out))
    print(f"New draft v{new.version} (UNLOCKED, no rulings yet): {rpath}\nReport: {cpath}\n"
          f"Questionnaire ({len(rep['questionnaire']['questions'])} questions): {Path(args.out) / 'questionnaire.md'}\n"
          f"Answer it once, then `essay-grader apply-answers --rubric {rpath} --questionnaire {Path(args.out) / 'questionnaire.json'} "
          f"--answers answers.yaml` and `essay-grader lock` the result.")
    print(f"Calibration sample ids recorded in rubric: {len(new.calibration.sample_ids)}")
    return 0


def _load_expected(path):
    if not path:
        return None
    from .citations import load_expected

    return load_expected(path)


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
        graders.append(_decisions(args))
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
                      check_citations=args.check_citations,
                      expected_authorities=_load_expected(args.expected_authorities),
                      agreement=AgreementConfig(args.disagreement_threshold, args.low_confidence, args.combine))
    if args.run_id:
        cfg.run_id = args.run_id
    elif args.dry_run:
        cfg.run_id = "dryrun-" + cfg.run_id
    budget = _budget(args)
    d = asyncio.run(run_pipeline(rubric, extract_text(args.exam), answers, graders, cfg, budget=budget))
    meta = json.loads((d / "run_metadata.json").read_text(encoding="utf-8"))
    for dg in meta["degradations"]:
        print(f"NOTE degradation: {dg}", file=sys.stderr)
    if meta["status"] != "complete":
        print(f"STOPPED: budget exhausted with {meta['answers_remaining']} answer(s) left; finished work is saved in {d}.\n"
              f"Resume: re-run the same command with --run-id {cfg.run_id} and a larger --budget-usd.")
        return 5
    print(f"Run complete: {d} (spent ${budget.spent_usd:.2f})\nSuggestions only; see review_queue.csv and agreement_report.md.")
    return 0


def cmd_citations_check(args) -> int:
    from . import citations

    text = extract_text(args.file)
    client = citations.CourtListenerClient(cache=citations.LookupCache(args.cache) if args.cache else None)
    expected = citations.load_expected(args.expected) if args.expected else None
    rep = citations.check(text, client=client, expected=expected)
    md = rep.to_markdown(title=f"Citation check: {Path(args.file).name}")
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        Path(args.out).with_suffix(".json").write_text(json.dumps(rep.to_dict(), indent=2), encoding="utf-8")
        print(f"Report: {args.out} (+ .json)")
    else:
        print(md)
    return 6 if rep.fabricated_suspects and args.fail_on_suspect else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="essay-grader", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, exam=True):
        if exam:
            sp.add_argument("--exam", required=True, help="exam question file (pdf/docx/txt)")
        sp.add_argument("--model", default=DEFAULT_CLAUDE_MODEL)
        sp.add_argument("--dry-run", action="store_true", help="offline fake clients; no API calls")
        sp.add_argument("--budget-usd", type=float, default=None, help="dollar cap for this command (default: no cap)")

    h = sub.add_parser("harden", help="stress-test a DRAFT rubric before any student exam is seen")
    common(h)
    h.add_argument("--syllabus", required=True)
    h.add_argument("--rubric", required=True)
    h.add_argument("--out", default="runs")
    h.set_defaults(budget_usd=5.0)
    h.add_argument("--n", type=int, default=None, help="archetypes per question (default: scaled to the exam, up to 11)")
    h.add_argument("--k", type=int, default=5, help="OpenAI Decisions repeats per synthetic answer")
    h.add_argument("--expected-words", type=int, default=800)
    h.add_argument("--max-questions", type=int, default=12)
    h.add_argument("--no-decisions", action="store_true", help="skip the Decisions variance repeats")
    h.add_argument("--openai-model", default="gpt-6-luna")
    h.add_argument("--decision-threshold", type=float, default=0.5)
    h.set_defaults(func=cmd_harden)

    aa = sub.add_parser("apply-answers", help="apply the professor's one-shot questionnaire answers to a draft rubric")
    aa.add_argument("--rubric", required=True, help="the DRAFT rubric the questionnaire was generated for")
    aa.add_argument("--questionnaire", required=True, help="questionnaire.json")
    aa.add_argument("--answers", required=True, help="answers.yaml: question id -> option label or custom ruling text")
    aa.add_argument("--out", help="new draft path (default: <stem>.v<N>.yaml next to the rubric)")
    aa.add_argument("--allow-stale", action="store_true", help="apply even if the rubric hash differs from the questionnaire's")
    aa.set_defaults(func=cmd_apply_answers)

    l = sub.add_parser("lock", help="freeze a rubric with a sha256 content hash")
    l.add_argument("rubric")
    l.add_argument("--out")
    l.set_defaults(func=cmd_lock)

    c = sub.add_parser("calibrate", help="propose rulings from a small anonymized sample (creates a new rubric version)")
    common(c)
    c.add_argument("--rubric", required=True)
    c.add_argument("--sample", required=True)
    c.add_argument("--out", default="runs/calibration")
    c.add_argument("--max-questions", type=int, default=12)
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

    ci = sub.add_parser("citations", help="extract and verify legal citations (CourtListener); statutes are extracted only")
    ci_sub = ci.add_subparsers(dest="citations_cmd", required=True)
    cc = ci_sub.add_parser("check", help="check the citations in a text/pdf/docx file")
    cc.add_argument("file")
    cc.add_argument("--expected", help="expected.yaml: instructor list of authorities (names or cites)")
    cc.add_argument("--out", help="write the markdown report here (and JSON beside it); default prints markdown")
    cc.add_argument("--cache", help="sqlite cache path (default ~/.cache/essay_grader/citations.sqlite or $ESSAY_GRADER_CACHE)")
    cc.add_argument("--fail-on-suspect", action="store_true", help="exit 6 if any citation is fabricated_suspect")
    cc.set_defaults(func=cmd_citations_check)
    g.add_argument("--check-citations", action="store_true", help="verify citations in each answer (adds review reasons only)")
    g.add_argument("--expected-authorities", help="expected.yaml for --check-citations")

    from .study.cli import add_study_parser  # "AI takes the exam" subcommand group; see docs/STUDY.md

    add_study_parser(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RubricNotLocked as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except BudgetExceeded as e:
        print(f"error: budget exceeded: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
