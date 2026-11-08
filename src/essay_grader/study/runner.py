"""Cell runner: component x arm x model, ONE independent API call per cell, budget-aware, resumable, verbatim capture.

Isolation is structural. For each cell the runner builds a fresh `GenRequest` from (a) the rendered protocol prompt for the
cell's component and (b) the materials bundle of the cell's arm only (loaded through `ArmSandbox`). There is no shared
conversation object, and provider calls take a request, not a session. Cells run concurrently under a semaphore.

Resume: a cell with a capture AND a meta file is skipped, after checking that the captured prompt hash, materials manifest,
requested model and protocol fingerprint still match the design (else `StaleCaptureError`; nothing is re-spent silently).
Failed or refused cells leave an `.error.json` and are retried on the next run. When the budget runs out the run stops
cleanly; finished captures are kept.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .. import __version__ as TOOL_VERSION
from ..budget import Budget, BudgetExceeded
from ..llm import PRICE_PER_MTOK, price_for
from .design import Cell, Design
from .materials import MaterialsBundle, ensure_materials_naive, sandbox_for
from .protocol import Protocol, sha256_text
from .providers import GenRequest, Provider, make_provider
from .store import StaleCaptureError, StudyStore


class UnpricedModelError(RuntimeError):
    pass


@dataclass
class RunSummary:
    captured: list[str] = field(default_factory=list)
    resumed_skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped_budget: list[str] = field(default_factory=list)
    served_model_differs: dict[str, str] = field(default_factory=dict)
    spent_usd: float = 0.0

    @property
    def status(self) -> str:
        if self.skipped_budget:
            return "budget_exhausted"
        return "errors" if self.failed else "complete"

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["status"] = self.status
        return d


def register_prices(design: Design) -> list[str]:
    """Register design-supplied prices with llm.PRICE_PER_MTOK (so Budget can gate them); return models still unpriced."""
    for m in design.models:
        if m.price_per_mtok is not None:
            PRICE_PER_MTOK[m.model] = tuple(m.price_per_mtok)  # type: ignore[assignment]
    return sorted({m.model for m in design.models if price_for(m.model) is None})


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_cell_request(design: Design, protocol: Protocol, cell: Cell, bundle: MaterialsBundle, prompt: str) -> GenRequest:
    comp, model = design.component(cell.component), design.model(cell.model)
    return GenRequest(
        cell_id=cell.id, model=model.model, system=bundle.render(), user=prompt,
        max_tokens=design.max_tokens_for(comp, model), effort=design.effort_for(comp, model), purpose=f"study:{cell.id}",
        hint={"kind": comp.kind, "word_ceiling": comp.word_ceiling, "n_questions": comp.n_questions})


def _check_not_stale(store: StudyStore, cell: Cell, req: GenRequest, bundle: MaterialsBundle, protocol: Protocol) -> None:
    meta = store.read_meta(cell)
    problems = []
    if meta.get("request_sha256") != req.request_sha256():
        problems.append("rendered request (prompt + materials) changed")
    if meta.get("materials", {}).get("manifest_sha256") != bundle.manifest_sha256:
        problems.append("materials manifest changed")
    if meta.get("requested_model") != req.model:
        problems.append(f"model changed ({meta.get('requested_model')} -> {req.model})")
    fp = protocol.fingerprint()
    if meta.get("prompt_version") != fp["prompt_version"] or meta.get("template_sha256") != fp["template_sha256"]:
        problems.append("protocol prompt_version/template hashes changed")
    if problems:
        raise StaleCaptureError(
            f"cell {cell.id} was captured under different inputs: {'; '.join(problems)}. Verbatim captures are never "
            f"overwritten: move the old capture/meta files aside deliberately (and log why) to regenerate this cell.")


async def run_study(design: Design, store: StudyStore, providers: dict[str, Provider], *, budget: Budget | None = None,
                    concurrency: int = 4, protocol: Protocol | None = None, allow_unpriced: bool = False,
                    only: Callable[[Cell], bool] | None = None) -> RunSummary:
    protocol = protocol or Protocol.load(design.path(design.protocol))
    ensure_materials_naive(design)  # blocks the run on any blocking finding

    unpriced = register_prices(design)
    if unpriced and budget is not None and budget.max_usd != float("inf") and not allow_unpriced:
        raise UnpricedModelError(f"no price known for {unpriced}: a finite budget cannot gate them. Add price_per_mtok to the "
                                 f"model in the design (or pass allow_unpriced=True to run ungated).")

    # materials are loaded once per arm, through the sandbox; cells only ever see their own arm's bundle
    bundles = {a.id: sandbox_for(design, a.id).load() for a in design.arms}
    prompts = {c.id: protocol.render(c.kind, course=design.exam.course, question_text=design.question_text(c))
               for c in design.components}

    cells = [c for c in design.cells() if only is None or only(c)]
    requests = {c.id: build_cell_request(design, protocol, c, bundles[c.arm], prompts[c.component]) for c in cells}

    summary = RunSummary()
    todo: list[Cell] = []
    for c in cells:
        if store.is_captured(c):
            _check_not_stale(store, c, requests[c.id], bundles[c.arm], protocol)
            summary.resumed_skipped.append(c.id)
        else:
            todo.append(c)

    lock = store.root / "study.lock.json"
    if not lock.exists():
        store.root.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({
            "study_id": design.study_id, "created_at": _utc(), "tool_version": TOOL_VERSION, "protocol": protocol.fingerprint(),
            "design": json.loads(design.model_dump_json()),
            "materials": {a: {"manifest_sha256": b.manifest_sha256, "files": b.manifest} for a, b in bundles.items()},
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    sem = asyncio.Semaphore(max(1, concurrency))
    stop = asyncio.Event()

    async def run_one(cell: Cell) -> None:
        async with sem:
            if stop.is_set():
                summary.skipped_budget.append(cell.id)
                return
            req = requests[cell.id]
            model = design.model(cell.model)
            provider = providers.get(model.provider)
            if provider is None:
                summary.failed[cell.id] = f"no provider instance for {model.provider!r}"
                return
            store.prompt_path(cell).write_text(prompts[cell.component], encoding="utf-8")
            started = _utc()
            try:
                res = await provider.generate(req, budget=budget)
            except BudgetExceeded as e:
                stop.set()
                summary.skipped_budget.append(cell.id)
                return
            except Exception as e:  # provider/transport failure: recorded, retried on resume
                summary.failed[cell.id] = f"{type(e).__name__}: {e}"
                store.write_error(cell, {"cell": cell.id, "error": summary.failed[cell.id], "at": _utc()})
                return
            finished = _utc()
            cost = res.usage.estimated_cost(res.served_model) if price_for(res.served_model) else res.usage.estimated_cost(req.model)
            if not res.ok:
                summary.failed[cell.id] = res.error or "no text returned"
                store.write_error(cell, {"cell": cell.id, "error": res.error, "refused": res.refused, "served_model": res.served_model,
                                         "usage": res.usage.to_dict(), "started_at": started, "finished_at": finished})
                return
            comp = design.component(cell.component)
            differs = not res.served_model.startswith(req.model)
            meta = {
                "schema": 1, "cell_id": cell.id, "component": cell.component, "kind": comp.kind, "arm": cell.arm,
                "model_key": cell.model, "provider": model.provider, "requested_model": req.model,
                "served_model": res.served_model, "model_fallback": differs, "examid": design.examid_for(cell.arm, cell.model),
                "protocol_id": protocol.protocol_id, "prompt_version": protocol.prompt_version,
                "template_sha256": protocol.current_hashes(), "prompt_sha256": sha256_text(prompts[cell.component]),
                "request_sha256": req.request_sha256(),
                "materials": {"arm": cell.arm, "manifest": bundles[cell.arm].manifest,
                              "manifest_sha256": bundles[cell.arm].manifest_sha256},
                "effort": req.effort, "max_tokens": req.max_tokens, "started_at": started, "finished_at": finished,
                "usage": res.usage.to_dict(), "estimated_cost_usd": cost, "stop_reason": res.stop_reason,
                "truncated": res.truncated, "tool_version": TOOL_VERSION, "words": len((res.text or "").split()),
            }
            store.write_capture(cell, res.text or "", meta)
            summary.captured.append(cell.id)
            if differs:
                summary.served_model_differs[cell.id] = f"{req.model} -> {res.served_model}"

    await asyncio.gather(*(run_one(c) for c in todo))
    summary.spent_usd = round(budget.spent_usd, 6) if budget else 0.0
    store.root.mkdir(parents=True, exist_ok=True)
    with open(store.root / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _utc(), **summary.to_dict(), "budget": budget.summary() if budget else None}) + "\n")
    return summary


def providers_for(design: Design, *, dry_run: bool) -> dict[str, Provider]:
    return {name: make_provider(name, dry_run=dry_run) for name in sorted({m.provider for m in design.models})}
