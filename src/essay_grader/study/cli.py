"""`essay-grader study ...`: validate | check-materials | run | audit | revise | package | ingest-scores | report."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from ..budget import Budget, BudgetExceeded

EXIT_USAGE, EXIT_BUDGET, EXIT_STOPPED, EXIT_NAIVETY, EXIT_PROTOCOL, EXIT_BLOCKED = 2, 4, 5, 7, 8, 9


def _load(args):
    from .design import Design

    return Design.load(args.design)


def _store(args, design):
    from .store import StudyStore

    out = Path(args.out) / ("dryrun" if getattr(args, "dry_run", False) else "")
    return StudyStore(out, design)


def _budget(args) -> Budget:
    usd = getattr(args, "budget_usd", None)
    return Budget(max_usd=usd) if usd is not None else Budget.unlimited()


def _bundles(design):
    from .materials import sandbox_for

    return {a.id: sandbox_for(design, a.id).load() for a in design.arms}


def cmd_validate(args) -> int:
    from .design import validate_design

    rep = validate_design(_load(args))
    print(rep.render())
    return 0 if rep.ok else EXIT_USAGE


def cmd_check_materials(args) -> int:
    from .materials import check_materials

    rep = check_materials(_load(args))
    print(rep.render())
    if args.json:
        Path(args.json).write_text(json.dumps(rep.to_dict(), indent=2), encoding="utf-8")
    return 0 if rep.passed else EXIT_NAIVETY


def cmd_run(args) -> int:
    from .audit import audit_study
    from .protocol import Protocol
    from .record import write_run_records
    from .runner import providers_for, run_study
    from .design import validate_design

    design = _load(args)
    rep = validate_design(design)
    if not rep.ok:
        print(rep.render(), file=sys.stderr)
        return EXIT_USAGE
    store = _store(args, design)
    protocol = Protocol.load(design.path(design.protocol))
    budget = _budget(args)
    providers = providers_for(design, dry_run=args.dry_run)
    summary = asyncio.run(run_study(design, store, providers, budget=budget, concurrency=args.concurrency, protocol=protocol,
                                    allow_unpriced=args.allow_unpriced))
    for cid, why in sorted(summary.failed.items()):
        print(f"FAILED {cid}: {why}", file=sys.stderr)
    for cid, what in sorted(summary.served_model_differs.items()):
        print(f"WARNING {cid}: served model differs from the requested one ({what}); recorded in the capture metadata", file=sys.stderr)
    audits = audit_study(design, store, protocol, citations=not args.dry_run)
    from .materials import check_materials

    write_run_records(design, store, protocol, _bundles(design), summary=summary.to_dict(), audits=audits,
                      naivety=check_materials(design), stage="generation")
    print(f"Captured {len(summary.captured)}, resumed/skipped {len(summary.resumed_skipped)}, failed {len(summary.failed)}, "
          f"skipped for budget {len(summary.skipped_budget)}; spent ${summary.spent_usd:.2f}.\n"
          f"Output: {store.root}  (verbatim captures are read-only; next: `study audit`, `study revise`, `study package`)")
    if summary.status == "budget_exhausted":
        print(f"STOPPED: budget exhausted with {len(summary.skipped_budget)} cell(s) left; finished captures are saved. "
              f"Re-run the same command with a larger --budget-usd to resume.")
        return EXIT_STOPPED
    return 1 if summary.failed else 0


def cmd_audit(args) -> int:
    from .audit import audit_study
    from .protocol import Protocol

    design = _load(args)
    store, protocol = _store(args, design), Protocol.load(design.path(design.protocol))
    results = audit_study(design, store, protocol, revised=args.revised, citations=not args.no_citations)
    bad = 0
    for r in results:
        status = "pass" if r.passed else f"FAIL ({len(r.violations)})"
        print(f"{r.cell_id:<40} {r.target:<8} {status:<10} words={r.words} citations={r.citations.get('status')}")
        for f in r.violations[:6]:
            print(f"    {f.check}: {f.message}" + (f" [line {f.line}]" if f.line else ""))
        bad += not r.passed
    print(f"{len(results)} cell(s) audited, {bad} with violations. JSON in {store.root / 'audits'}")
    return 1 if (bad and args.strict) else 0


def cmd_revise(args) -> int:
    from .protocol import Protocol
    from .revise import revise_cell
    from .runner import providers_for

    design = _load(args)
    store, protocol = _store(args, design), Protocol.load(design.path(design.protocol))
    providers = providers_for(design, dry_run=args.dry_run) if args.llm else {}
    budget = _budget(args)

    async def go():
        outs = []
        for cell in store.captured_cells():
            if args.cell and cell.id not in args.cell:
                continue
            prov = providers.get(design.model(cell.model).provider)
            outs.append(await revise_cell(design, store, cell, protocol, use_llm=args.llm, provider=prov, budget=budget,
                                          reason=args.reason or "", dash_mode=args.dash_mode, citations=not args.dry_run))
        return outs

    outs = asyncio.run(go())
    for o in outs:
        print(f"{o.cell_id:<40} {'REVISED' if o.revised else 'unchanged'}  {o.reason}")
        for h in o.human_review:
            print(f"    needs human: {h}")
    print("Verbatim captures were not modified. Revisions: " + str(store.root / "revisions"))
    return 0


def cmd_package(args) -> int:
    from .audit import audit_study
    from .package import package_study
    from .protocol import Protocol
    from .record import write_run_records

    design = _load(args)
    store, protocol = _store(args, design), Protocol.load(design.path(design.protocol))
    res = package_study(design, store, protocol, allow_unrevised_violations=args.allow_violations, build_pdfs=not args.no_pdf)
    for n in res.notes:
        print(f"NOTE: {n}")
    for f in res.files:
        print(f"wrote {f}")
    print(f"{len(res.tex_files)} .tex source(s) in {store.root / 'package' / 'tex_private'}; deliverables dir: {res.deliver_dir}")
    audits = audit_study(design, store, protocol, citations=False)
    write_run_records(design, store, protocol, _bundles(design), audits=audits, status="packaged", stage="packaging")
    return 0


def cmd_ingest(args) -> int:
    from .record import write_scoring_records
    from .report import write_reports
    from .scoring import analyze, csv_rows, mcq_scores, read_scores

    design = _load(args)
    store = _store(args, design)
    headers, rows = read_scores(args.scores)
    comp_cols = dict(kv.split("=", 1) for kv in (args.component_column or []))
    flags = dict(kv.split("=", 1) for kv in (args.ai_flag or []))
    try:
        det = mcq_scores(design, store)
    except Exception as e:  # deterministic MCQ is a convenience; missing key/captures must not block faculty scoring
        print(f"NOTE: deterministic MCQ scoring skipped ({e})", file=sys.stderr)
        det = {}
    analysis = analyze(design, headers, rows, id_column=args.id_column, total_column=args.total_column,
                       component_columns=comp_cols, ai_flags=flags, mcq_det=det)
    d = store.d("scoring")
    (d / "scoring-analysis.json").write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_scoring_records(design, store, analysis, csv_rows(design, analysis))
    write_reports(design, store, analysis)
    tot = analysis["metrics"].get("total", {}).get("class", {})
    print(f"Real students (AI IDs excluded): n = {tot.get('n')}; AI rows scored: "
          f"{sum(1 for e in analysis['ai_ids'] if e not in analysis['missing_ai_ids'])}/{len(analysis['ai_ids'])}.")
    for n in analysis["notes"]:
        print(f"NOTE: {n}")
    print(f"Wrote {d / 'scoring-analysis.json'}, methodology/scoring-results.csv, report.md, methodology/cohort-results.draft.md "
          f"under {store.root}. Real-student rows are NOT copied anywhere.")
    return 0


def cmd_report(args) -> int:
    from .report import write_reports

    design = _load(args)
    store = _store(args, design)
    ap = store.root / "scoring" / "scoring-analysis.json"
    analysis = json.loads(ap.read_text(encoding="utf-8")) if ap.is_file() else None
    for p in write_reports(design, store, analysis):
        print(f"wrote {p}")
    return 0


def _g(f):
    return lambda a: run_guarded(f, a)


def add_study_parser(sub) -> None:
    st = sub.add_parser("study", help="AI takes the exam: design, run cells, audit, revise, package, ingest scores, report")
    ssub = st.add_subparsers(dest="study_cmd", required=True)

    def common(sp, *, out=True, dry=True, budget=False):
        sp.add_argument("design", help="study design YAML")
        if out:
            sp.add_argument("--out", default="runs/study", help="study output root (default runs/study; git-ignored)")
        if dry:
            sp.add_argument("--dry-run", action="store_true", help="fake providers; outputs go to <out>/dryrun/")
        if budget:
            sp.add_argument("--budget-usd", type=float, default=None, help="dollar cap (default: no cap)")

    v = ssub.add_parser("validate", help="static checks of the design (no API)")
    common(v, out=False, dry=False)
    v.set_defaults(func=_g(cmd_validate))

    m = ssub.add_parser("check-materials", help="materials-naivety check (blocks `run` on hits)")
    common(m, out=False, dry=False)
    m.add_argument("--json", help="also write the findings as JSON")
    m.set_defaults(func=_g(cmd_check_materials))

    r = ssub.add_parser("run", help="run every cell (resumable); verbatim capture")
    common(r, budget=True)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--allow-unpriced", action="store_true", help="run a model with no known price even under a finite budget")
    r.set_defaults(func=_g(cmd_run))

    a = ssub.add_parser("audit", help="automated audit of the captures")
    common(a)
    a.add_argument("--revised", action="store_true", help="audit the revised pass instead of the captures")
    a.add_argument("--no-citations", action="store_true", help="skip the essay_grader.citations hook")
    a.add_argument("--strict", action="store_true", help="exit 1 if any cell has violations")
    a.set_defaults(func=_g(cmd_audit))

    rv = ssub.add_parser("revise", help="separate, recorded revision pass (never touches captures)")
    common(rv, budget=True)
    rv.add_argument("--llm", action="store_true", help="also allow ONE constrained LLM call per cell for substantive violations")
    rv.add_argument("--reason", help="free-text reason recorded in each revision record")
    rv.add_argument("--dash-mode", choices=["comma", "en"], default="comma")
    rv.add_argument("--cell", action="append", help="limit to this cell id (repeatable)")
    rv.set_defaults(func=_g(cmd_revise))

    p = ssub.add_parser("package", help="Examplify PDFs (essays) + registrar-style MCQ workbook")
    common(p)
    p.add_argument("--no-pdf", action="store_true", help="write the .tex sources only")
    p.add_argument("--allow-violations", action="store_true", help="package even if a cell fails the audit (not recommended)")
    p.set_defaults(func=_g(cmd_package))

    i = ssub.add_parser("ingest-scores", help="faculty raw scores CSV -> class stats, AI z/rank, arm contrasts")
    common(i, dry=True)
    i.add_argument("--scores", required=True, help="faculty raw scores CSV (one row per ExamID)")
    i.add_argument("--id-column")
    i.add_argument("--total-column")
    i.add_argument("--component-column", action="append", help="component_id=CSV column (repeatable)")
    i.add_argument("--ai-flag", action="append", help="EXAMID=yes|no faculty AI-flag observation (repeatable)")
    i.set_defaults(func=_g(cmd_ingest))

    rp = ssub.add_parser("report", help="study report + cohort dashboard draft")
    common(rp)
    rp.set_defaults(func=_g(cmd_report))


def run_guarded(func, args) -> int:
    """Map study exceptions to exit codes so cli.main needs no changes."""
    from .design import DesignError
    from .materials import MaterialsBoundaryError, MaterialsError, MaterialsNaivetyError
    from .package import PackagingError
    from .protocol import ProtocolDriftError, ProtocolError
    from .runner import UnpricedModelError
    from .scoring import ScoringError
    from .store import CaptureTamperedError, StaleCaptureError

    try:
        return func(args)
    except DesignError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except MaterialsNaivetyError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NAIVETY
    except (MaterialsBoundaryError, MaterialsError) as e:
        print(f"error: materials: {e}", file=sys.stderr)
        return EXIT_NAIVETY
    except ProtocolDriftError as e:
        print(f"error: protocol drift: {e}", file=sys.stderr)
        return EXIT_PROTOCOL
    except ProtocolError as e:
        print(f"error: protocol: {e}", file=sys.stderr)
        return EXIT_PROTOCOL
    except (StaleCaptureError, CaptureTamperedError, PackagingError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_BLOCKED
    except UnpricedModelError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_BUDGET
    except ScoringError as e:
        print(f"error: scoring: {e}", file=sys.stderr)
        return EXIT_USAGE
    except BudgetExceeded as e:
        print(f"error: budget exceeded: {e}", file=sys.stderr)
        return EXIT_BUDGET
