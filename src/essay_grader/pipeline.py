"""Dual-grade pipeline: locked rubric in, suggestions + review queue out. AI never finalizes a grade."""
from __future__ import annotations

import asyncio
import csv
import json
import random
from dataclasses import dataclass, field
from typing import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .agreement import AgreementConfig, evaluate, run_stats
from .budget import Budget, BudgetExceeded
from .graders.base import Grader, GraderResult
from .ingest import Answer, apply_length_policy
from .llm import DEFAULT_CLAUDE_MODEL, SONNET_MODEL, Usage
from .prompts import prompt_hashes
from .rubric import Rubric


@dataclass
class GradeConfig:
    run_id: str = field(default_factory=lambda: "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out_dir: Path = Path("runs")
    seed: int = 0
    concurrency: int = 4
    agreement: AgreementConfig = field(default_factory=AgreementConfig)
    exclude_calibration: bool = False
    dry_run: bool = False
    # Optional citation verification (off by default). Adds a `citations` report to each result and a review reason
    # when a citation looks fabricated. It NEVER changes points or suggested totals; the professor decides.
    check_citations: bool = False
    citation_checker: Callable[[str], object] | None = None  # default: essay_grader.citations.check
    expected_authorities: list | None = None  # instructor list for expected-authorities matching

    @property
    def run_dir(self) -> Path:
        return Path(self.out_dir) / self.run_id


def _key(rec: dict) -> tuple[str, str]:
    return rec["student_id"], rec["question_id"]


def load_results(path: Path) -> dict[tuple[str, str], dict]:
    """Last record per (student, question) wins (re-graded errors append later)."""
    out: dict[tuple[str, str], dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                out[_key(rec)] = rec
    return out


def _complete(rec: dict, grader_names: list[str]) -> bool:
    res = rec.get("results", {})
    return all(n in res and not res[n].get("error") for n in grader_names)


async def _safe_grade(g: Grader, rubric: Rubric, qid: str, exam: str, text: str, budget: Budget) -> GraderResult:
    try:
        return await g.grade(rubric, rubric.question(qid), exam, text, budget=budget)
    except BudgetExceeded:
        raise  # not a grader error: the pipeline stops cleanly and the answer stays pending
    except Exception as e:  # recorded, never raised: a failed grader sends the answer to human review
        return GraderResult(grader=g.name, model=g.model, question_id=qid, error=f"{type(e).__name__}: {e}"[:500])


async def _citations(text: str, cfg: GradeConfig):
    """Run the (blocking, possibly networked) citation check off the event loop. A failure here must not lose the
    grades: on failure return the error string; the record gets `citations_error` and no `citations`."""
    from . import citations

    fn = cfg.citation_checker
    try:
        if fn is not None:
            return await asyncio.to_thread(fn, text)
        return await asyncio.to_thread(lambda: citations.check(text, expected=cfg.expected_authorities))
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:300]


def _check_metadata(path: Path, rubric: Rubric, graders: list[Grader], cfg: GradeConfig) -> dict | None:
    if not path.exists():
        return None
    old = json.loads(path.read_text(encoding="utf-8"))
    # `requested_model` is what the caller asked for; `model` is what was actually used (after any budget downgrade)
    same = (old["rubric"]["hash"] == rubric.content_hash and old["seed"] == cfg.seed
            and [(g["name"], g.get("requested_model", g["model"])) for g in old["graders"]] == [(g.name, g.model) for g in graders])
    if not same:
        raise RuntimeError(f"run {cfg.run_id} exists with different rubric/seed/graders/models; use a new --run-id")
    return old


def _estimate_pending(graders: list[Grader], rubric: Rubric, exam_text: str, pending: list[tuple[int, Answer]],
                      worst_case: bool = False) -> float:
    total = 0.0
    for _, a in pending:
        text, _info = apply_length_policy(a.text, rubric.policies.length_policy)
        for g in graders:
            est = getattr(g, "estimate_cost", None)
            if est is not None:
                total += est(rubric, rubric.question(a.question_id), exam_text, text, worst_case=worst_case)
    return total


async def run_pipeline(rubric: Rubric, exam_text: str, answers: list[Answer], graders: list[Grader],
                       cfg: GradeConfig, *, budget: Budget | None = None) -> Path:
    """Grade with a locked rubric. With a `budget`: estimate the whole run first (auto-downgrading Claude Opus -> Sonnet
    if needed, else raising BudgetExceeded), and stop cleanly mid-run when the budget is exhausted; re-running with the
    same run id resumes. Every degradation is recorded in run_metadata.json."""
    rubric.require_locked()
    if not graders:
        raise ValueError("at least one grader required")
    budget = budget if budget is not None else Budget.unlimited()
    run_dir = cfg.run_dir
    results_path = run_dir / "results.jsonl"
    old_meta = _check_metadata(run_dir / "run_metadata.json", rubric, graders, cfg)
    requested = {g.name: g.model for g in graders}
    degradations: list[dict] = list(old_meta.get("degradations", [])) if old_meta else []
    if old_meta:  # resume with the models actually used so far: one run, one model per grader
        eff = {g["name"]: g["model"] for g in old_meta["graders"]}
        graders = [g.with_model(eff[g.name]) if hasattr(g, "with_model") and eff[g.name] != g.model else g for g in graders]
    started = old_meta["started_at"] if old_meta else datetime.now(timezone.utc).isoformat(timespec="seconds")
    names = [g.name for g in graders]
    calib = set(rubric.calibration.sample_ids) if rubric.calibration else set()
    if cfg.exclude_calibration:
        answers = [a for a in answers if a.student_id not in calib]

    # randomized grading order, reproducible from the recorded seed
    ordered = sorted(answers, key=lambda a: (a.student_id, a.question_id))
    random.Random(cfg.seed).shuffle(ordered)
    done = load_results(results_path)
    pending = [(i, a) for i, a in enumerate(ordered)
               if (a.student_id, a.question_id) not in done or not _complete(done[(a.student_id, a.question_id)], names)]

    # full pre-run estimate; auto-downgrade Claude Opus -> Sonnet, else refuse (no prompt)
    estimate = _estimate_pending(graders, rubric, exam_text, pending)
    if estimate > budget.remaining() + 1e-12:
        swapped = [g.with_model(SONNET_MODEL) if g.name == "claude" and g.model == DEFAULT_CLAUDE_MODEL and hasattr(g, "with_model") else g
                   for g in graders]
        new_estimate = _estimate_pending(swapped, rubric, exam_text, pending)
        downgraded = swapped != graders
        if downgraded:
            graders = swapped
            degradations.append({"kind": "grader_model_downgrade", "grader": "claude", "from": DEFAULT_CLAUDE_MODEL,
                                 "to": SONNET_MODEL, "estimate_before_usd": round(estimate, 4),
                                 "estimate_after_usd": round(new_estimate, 4), "remaining_usd": round(budget.remaining(), 4),
                                 "reason": "estimated run cost exceeded the remaining budget"})
        estimate = new_estimate
        if estimate > budget.remaining() + 1e-12:
            raise BudgetExceeded(
                f"estimated cost ${estimate:.2f} for {len(pending)} answer(s) exceeds the remaining budget "
                f"${budget.remaining():.2f}" + (" even after downgrading Claude to " + SONNET_MODEL if downgraded else "")
                + "; raise the budget or grade fewer answers", estimate_usd=estimate, remaining_usd=budget.remaining())
    run_dir.mkdir(parents=True, exist_ok=True)

    sem, wlock = asyncio.Semaphore(cfg.concurrency), asyncio.Lock()

    stopped = asyncio.Event()

    async def work(idx: int, a: Answer, fh) -> None:
        async with sem:
            if stopped.is_set():
                return
            # length policy is deterministic and applied BEFORE graders; graders never see the student id
            text, info = apply_length_policy(a.text, rubric.policies.length_policy)
            if not budget.can_afford(_estimate_pending(graders, rubric, exam_text, [(idx, a)], worst_case=True)):
                stopped.set()
                return
            rs = await asyncio.gather(*(_safe_grade(g, rubric, a.question_id, exam_text, text, budget) for g in graders),
                                      return_exceptions=True)
            if any(isinstance(r, BudgetExceeded) for r in rs):
                stopped.set()  # nothing is recorded for this answer; it stays pending for the resume
                return
            for r in rs:
                if isinstance(r, BaseException):
                    raise r
            results = {r.grader: r for r in rs}
            ev = evaluate(rubric.question(a.question_id), results, cfg.agreement)
            if ev["suggested_total"] is not None:
                ev["suggested_total"] = max(0.0, ev["suggested_total"] - info["deduction"])
            citation_report, cite_error = None, None
            if cfg.check_citations:
                citation_report = await _citations(a.text, cfg)
                if isinstance(citation_report, str):
                    cite_error, citation_report = citation_report, None
                if citation_report is not None and citation_report.fabricated_suspects:
                    # review reason only: points, criteria and suggested_total are left exactly as graded
                    ev["flags"] = [*ev["flags"], "citation:fabricated_suspect"]
                    ev["status"] = "needs_human_review"
            rec = {"student_id": a.student_id, "question_id": a.question_id, "source": a.source, "order_index": idx,
                   "words": info["words"], "words_graded": info["words_graded"], "truncated": info["truncated"],
                   "length_deduction": info["deduction"], "warnings": a.warnings,
                   "calibration_sample": a.student_id in calib,
                   "results": {n: r.model_dump() for n, r in results.items()}, **ev}
            if citation_report is not None:
                rec["citations"] = citation_report.to_dict()
            elif cite_error:
                rec["citations_error"] = cite_error
            async with wlock:
                fh.write(json.dumps(rec) + "\n")
                fh.flush()

    with results_path.open("a", encoding="utf-8") as fh:
        await asyncio.gather(*(work(i, a, fh) for i, a in pending))

    records = [r for r in load_results(results_path).values() if (r["student_id"], r["question_id"]) in
               {(a.student_id, a.question_id) for a in answers}]
    records.sort(key=lambda r: r["order_index"])
    _write_scores(run_dir / "scores.csv", rubric, records, names)
    _write_queue(run_dir / "review_queue.csv", records)
    stats = run_stats(records, rubric)
    (run_dir / "agreement_report.md").write_text(_report_md(rubric, records, stats, cfg), encoding="utf-8")
    finished = {(r["student_id"], r["question_id"]) for r in records if _complete(r, names)}
    remaining = sum(1 for a in answers if (a.student_id, a.question_id) not in finished)
    _write_metadata(run_dir / "run_metadata.json", rubric, graders, cfg, records, started, requested=requested,
                    degradations=degradations, budget=budget, stopped=stopped.is_set(), remaining=remaining)
    return run_dir


def _write_scores(path: Path, rubric: Rubric, records: list[dict], names: list[str]) -> None:
    cols = [(q.id, c.id) for q in rubric.questions for c in q.criteria]
    header = ["student_id", "status", "calibration_sample", "words", "length_deduction"]
    for q, c in cols:
        header += [f"{q}.{c}.{n}" for n in names] + [f"{q}.{c}.suggested"]
    header += ["suggested_total", "max_points", "flags"]
    by_student: dict[str, list[dict]] = {}
    for r in records:
        by_student.setdefault(r["student_id"], []).append(r)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for sid in sorted(by_student):
            recs = {r["question_id"]: r for r in by_student[sid]}
            row: list = [sid]
            statuses = {r["status"] for r in recs.values()}
            row.append("needs_human_review" if "needs_human_review" in statuses
                       else "single_grader" if "single_grader" in statuses else "agreed")
            row += [any(r["calibration_sample"] for r in recs.values()), sum(r["words"] for r in recs.values()),
                    sum(r["length_deduction"] for r in recs.values())]
            for q, c in cols:
                crit = next((x for x in recs[q]["criteria"] if x["criterion_id"] == c), None) if q in recs else None
                row += [(crit["points"].get(n) if crit else "") for n in names]
                row.append("" if not crit or crit["suggested"] is None else round(crit["suggested"], 3))
            totals = [r["suggested_total"] for r in recs.values()]
            row.append("" if any(t is None for t in totals) else round(sum(totals), 3))
            row.append(sum(rubric.question(q).max_points for q in recs))
            row.append(";".join(fl for r in recs.values() for fl in r["flags"]))
            w.writerow(row)


def _write_queue(path: Path, records: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["student_id", "question_id", "reasons", "calibration_sample", "warnings"])
        for r in records:
            if r["status"] == "needs_human_review":
                w.writerow([r["student_id"], r["question_id"], ";".join(r["flags"]), r["calibration_sample"],
                            ";".join(r["warnings"])])


def _fmt(x: float | None, pct: bool = False) -> str:
    return "n/a" if x is None else (f"{x:.0%}" if pct else f"{x:.2f}")


def _report_md(rubric: Rubric, records: list[dict], stats: list[dict], cfg: GradeConfig) -> str:
    n = len(records)
    cnt = {s: sum(r["status"] == s for r in records) for s in ("agreed", "needs_human_review", "single_grader")}
    L = [f"# Agreement report: {rubric.exam_id} v{rubric.version}", "",
         f"Run `{cfg.run_id}`{' (DRY RUN: fake graders, meaningless scores)' if cfg.dry_run else ''}. "
         f"Rubric hash `{(rubric.content_hash or '')[:16]}`. All scores are SUGGESTIONS for human review.", "",
         f"- Answers graded: {n}", f"- Agreed: {cnt['agreed']}", f"- Needs human review: {cnt['needs_human_review']}",
         f"- Single grader (unchecked): {cnt['single_grader']}",
         f"- Answers with failed Claude quote validation: {sum(any(f.startswith('invalid_quotes') for f in r['flags']) for r in records)}",
         f"- Answers with grader errors: {sum(any(f.startswith('grader_error') for f in r['flags']) for r in records)}",
         f"- Name-like header warnings: {sum(bool(r['warnings']) for r in records)}",
         f"- Calibration-sample answers in run: {sum(r['calibration_sample'] for r in records)}", "",
         f"Disagreement threshold: {cfg.agreement.disagreement_threshold} pt (score criteria); label mismatch for binary/choice.", "",
         "## Per-criterion agreement", "",
         "| Question | Criterion | Kind | N | Exact agreement | Cohen's kappa | Mean abs diff (pts) |", "|---|---|---|---|---|---|---|"]
    for s in stats:
        L.append(f"| {s['question_id']} | {s['criterion_id']} | {s['kind']} | {s['n']} | {_fmt(s['exact_agreement'], True)} | {_fmt(s['kappa'])} | {_fmt(s['mean_abs_diff'])} |")
    L += ["", "Low-agreement criteria usually signal an ambiguous rubric item; consider a ruling and re-lock before the next run.", ""]
    return "\n".join(L)


def _write_metadata(path: Path, rubric: Rubric, graders: list[Grader], cfg: GradeConfig, records: list[dict],
                    started: str, *, requested: dict[str, str], degradations: list[dict], budget: Budget, stopped: bool,
                    remaining: int) -> None:
    usage: dict[tuple[str, str], Usage] = {}
    served: dict[str, set[str]] = {g.name: set() for g in graders}
    for r in records:
        for n, res in r["results"].items():
            usage.setdefault((n, res["model"]), Usage()).add(Usage.from_dict(res.get("usage") or {}))
            if res.get("served_model"):
                served[n].add(res["served_model"])
    meta = {
        "run_id": cfg.run_id, "tool_version": __version__, "dry_run": cfg.dry_run, "seed": cfg.seed,
        "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rubric": {"exam_id": rubric.exam_id, "version": rubric.version, "hash": rubric.content_hash,
                   "calibration_sample_ids": rubric.calibration.sample_ids if rubric.calibration else []},
        "status": "stopped_budget_exhausted" if stopped else "complete",
        "answers_remaining": remaining,
        "resume": ("Budget exhausted; finished answers are saved. Re-run with the same --run-id and more budget to resume."
                   if stopped else None),
        "degradations": degradations,
        "budget": budget.summary(),
        "graders": [{"name": g.name, "model": g.model, "requested_model": requested[g.name],
                     "served_models": sorted(served[g.name])} for g in graders],
        "prompt_hashes": prompt_hashes(),
        "config": {"concurrency": cfg.concurrency, "exclude_calibration": cfg.exclude_calibration,
                   "agreement": cfg.agreement.__dict__},
        "counts": {"answers": len(records)},
        "usage": [{"grader": n, "model": m, **u.to_dict(), "estimated_cost_usd": u.estimated_cost(m)}
                  for (n, m), u in sorted(usage.items())],
        "cost_note": "Estimated from llm.PRICE_PER_MTOK (Anthropic pricing as of Oct 2026); verify against billing. `budget` covers this invocation only.",
    }
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
