"""Methodology record generator. Writes into `<study>/methodology/`, in the formats exam-taker uses.

This is a RECORD GENERATOR: humans still curate. Every free-text field a human must fill is marked `[HUMAN: ...]`. Entries in
the markdown logs are append-only and idempotent (a marker comment keyed to the entry's content prevents duplicates on a
re-run; changed state appends a new entry). Generated text contains no em dashes (exam-taker's documentation discipline).

Files and their source formats (exam-taker `Spring 2026/methodology/`):
  per-exam-summary.csv   columns exactly as `_shared_tooling/sync_summary_md.py` REQUIRED_COLUMNS; `per-exam-summary.md` is
                         re-rendered from it (port of sync_summary_md.py)
  decision-log.md        schema: Context / Decision/event / Rationale / Protocol change (if any) / Cross-references
  materials-inventory.md schema: Course Materials/ contents / provided materials contents / Selection rationale / Gaps and notes
  format-variations.md   deltas against the Wave 1 canonical case, or "No deltas."
  prompt-evolution.md    schema: Change / Rationale / Triggered by / Affected exams
  scoring-results.csv    one row per AI ExamID; the real header is NOT in git (gitignored), so SCORING_COLUMNS below is
                         reconstructed from NEXT_SESSION_GUIDE Step 10. If a real file is placed in the methodology dir its
                         header is used and unknown columns are left blank.   # VERIFY against the real scoring-results.csv
"""
from __future__ import annotations

import csv
import hashlib
from datetime import date
from pathlib import Path

from .design import Design
from .materials import MaterialsBundle
from .protocol import Protocol
from .store import StudyStore

SUMMARY_COLUMNS = [
    "exam_date", "course", "professor", "format", "delivery", "n_essays", "n_mcq", "essay_word_ceilings", "arms_generated",
    "examids", "materials_provided_count", "materials_provided_summary", "generation_date", "status", "notes", "scoring_status",
    "class_n_real", "a_total", "a_rank", "b_total", "b_rank", "c_total", "c_rank",
]
SCORING_COLUMNS = [
    "exam_date", "course", "professor", "examid", "arm", "model", "scoring_method", "component_scores", "component_z",
    "total_score", "total_max", "class_rank", "class_n_real", "class_mean", "class_median", "class_sd", "z_score", "percentile",
    "faculty_ai_flag", "notes",
]
CANONICAL = {"format": "essay-only", "delivery": "in-class", "n_essays": 2, "ceilings": [1200, 1000], "arms": ["A", "B"]}


def mdir(store: StudyStore) -> Path:
    return store.d("methodology")


# ---- csv helpers --------------------------------------------------------------------------------------------------
def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    if not path.is_file():
        return [], []
    with path.open(newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        return list(rdr.fieldnames or []), list(rdr)


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


def upsert_row(path: Path, default_columns: list[str], row: dict, key: tuple[str, ...]) -> Path:
    cols, rows = _read_csv(path)
    cols = cols or default_columns
    key = tuple(c for c in key if c in cols) or key  # a pre-existing file may lack some key columns
    k = tuple(str(row.get(c, "")) for c in key)
    for existing in rows:
        if tuple(existing.get(c, "") for c in key) == k:
            existing.update({c: v for c, v in row.items() if c in cols and v != ""})
            break
    else:
        rows.append(row)
    _write_csv(path, cols, rows)
    return path


def render_summary_md(csv_path: Path, md_path: Path) -> None:
    """Port of sync_summary_md.py: the CSV is canonical, the .md is generated."""
    _, rows = _read_csv(csv_path)
    rows.sort(key=lambda r: r.get("exam_date") or "")
    head = ("<!-- Generated from per-exam-summary.csv. Do not edit directly. -->\n\n# Per-Exam Summary\n\n"
            "Generated rendering of `per-exam-summary.csv`. The CSV is canonical.\n\n")
    table = "| " + " | ".join(SUMMARY_COLUMNS) + " |\n|" + "|".join(["---"] * len(SUMMARY_COLUMNS)) + "|\n"
    for r in rows:
        table += "| " + " | ".join((r.get(c) or "").replace("|", "\\|").replace("\n", " ") for c in SUMMARY_COLUMNS) + " |\n"
    md_path.write_text(head + table, encoding="utf-8")


# ---- markdown append helpers ------------------------------------------------------------------------------------
def append_entry(path: Path, header: str, key: str, body: str) -> bool:
    """Append `body` unless an entry with the same content marker exists. Returns True if appended."""
    marker = f"<!-- study-record:{key}:{hashlib.sha256(body.encode()).hexdigest()[:10]} -->"
    existing = path.read_text(encoding="utf-8") if path.is_file() else header
    if marker in existing:
        if not path.is_file():
            path.write_text(existing, encoding="utf-8")
        return False
    path.write_text(existing.rstrip("\n") + "\n\n" + body.rstrip("\n") + "\n" + marker + "\n", encoding="utf-8")
    return True


_H_DECISION = ("# Decision Log (Wave 2, generated stubs)\n\nAppend-only. Generated by `essay-grader study`; entries contain "
               "`[HUMAN: ...]` placeholders that a person must complete or delete.\n\n## Entry schema\n\n"
               "Context / Decision/event / Rationale / Protocol change (if any) / Cross-references\n\n---\n\n## Entries\n")
_H_MATERIALS = ("# Materials Inventory (Wave 2, generated stubs)\n\nPer-exam record of operator-reference materials versus what "
                "each arm received.\n\n---\n\n## Entries\n")
_H_FORMAT = ("# Format Variations (Wave 2, generated stubs)\n\nDeltas against the canonical reference: the Wave 1 canonical reference exam, "
             "essay-only, in-class, two questions, ceilings 1,200/1,000, arms A and B.\n\n---\n\n## Per-exam variations\n")
_H_PROMPT = "# Prompt Evolution (Wave 2, generated)\n\nVersion history of the prompt templates.\n\n---\n\n## Entries\n"


def _today(today: date | None) -> str:
    return (today or date.today()).isoformat()


def _fmt_components(design: Design) -> str:
    return "; ".join(f"{c.id} ({c.kind}" + (f", ceiling {c.word_ceiling}" if c.word_ceiling else "")
                     + (f", {c.n_questions} questions" if c.n_questions else "") + ")" for c in design.components)


# ---- per-exam-summary row -----------------------------------------------------------------------------------------
def summary_row(design: Design, protocol: Protocol, bundles: dict[str, MaterialsBundle], *, status: str, generation_date: str) -> dict:
    ess, mcq = design.essays(), design.mcqs()
    fmt = "essay-only" if not mcq else ("MCQ-only" if not ess else "MCQ + essay")
    ids = "; ".join(f"{a}{'/' + m if len(design.models) > 1 else ''}={e}" for (a, m), e in design.examid_map().items())
    counts = {a: len(b.files) for a, b in bundles.items() if a != "A"}
    summ = "; ".join(f"{a}: " + ", ".join(f.rel for f in b.files) for a, b in bundles.items() if a != "A") or "none (A arm only)"
    models = ", ".join(f"{m.key}={m.model}" for m in design.models)
    notes = (f"Wave 2; protocol {protocol.protocol_id} prompt_version {protocol.prompt_version}; models: {models}; "
             f"arms: {', '.join(design.arm_ids)}; section {design.section}")
    return {
        "exam_date": design.exam.date or "", "course": design.exam.course, "professor": design.exam.professor, "format": fmt,
        "delivery": design.exam.delivery, "n_essays": len(ess), "n_mcq": sum(c.n_questions or 0 for c in mcq),
        "essay_word_ceilings": "; ".join(str(c.word_ceiling) if c.word_ceiling else "none" for c in ess),
        "arms_generated": ", ".join(design.arm_ids), "examids": ids, "materials_provided_count": max(counts.values(), default=0),
        "materials_provided_summary": summ, "generation_date": generation_date, "status": status, "notes": notes,
        "scoring_status": "pending",
    }


def write_run_records(design: Design, store: StudyStore, protocol: Protocol, bundles: dict[str, MaterialsBundle], *,
                      summary: dict | None = None, audits: list | None = None, status: str = "generated",
                      today: date | None = None, naivety=None, stage: str = "generation") -> list[Path]:
    """Steps 9.1-9.3 of the guide plus prompt-evolution and a decision-log stub. Safe to re-run."""
    d = mdir(store)
    day = _today(today)
    title = f"{design.exam.course} ({design.exam.professor})"
    out: list[Path] = []

    csvp = d / "per-exam-summary.csv"
    upsert_row(csvp, SUMMARY_COLUMNS, summary_row(design, protocol, bundles, status=status, generation_date=day),
               ("exam_date", "course", "professor"))
    render_summary_md(csvp, d / "per-exam-summary.md")
    out += [csvp, d / "per-exam-summary.md"]

    if stage != "generation":  # materials / format / prompt entries are written once, at generation time
        return out + _decision_stub(design, store, protocol, summary, audits, d, day, title, stage)

    # materials inventory
    lines = [f"## {day}: {title}", "", "**Course Materials/ contents (operator reference, not exposed to model):**",
             "- [HUMAN: list the operator-reference files/folders; this tool never sees them]", "",
             "**Provided materials per arm (exactly what each arm's cells received):**"]
    for arm in design.arms:
        b = bundles.get(arm.id)
        if arm.id == "A" or b is None or not b.files:
            lines.append(f"- Arm {arm.id}: no materials")
            continue
        lines.append(f"- Arm {arm.id} ({arm.label or arm.materials}; manifest sha256 {b.manifest_sha256[:12]}):")
        lines += [f"  - `{f.rel}` ({f.size:,} bytes, sha256 {f.sha256[:12]}): [HUMAN: description]" for f in b.files]
    lines += ["", "**Selection rationale:** [HUMAN: why these files]",
              "**Gaps and notes:** " + (naivety.render().splitlines()[0] if naivety is not None else "[HUMAN: naivety check not attached]")
              + " [HUMAN: gaps in materials availability and their implications for treatment-intensity comparability]"]
    append_entry(d / "materials-inventory.md", _H_MATERIALS, f"{design.study_id}:materials", "\n".join(lines))
    out.append(d / "materials-inventory.md")

    # format variations
    deltas = []
    mcq_n = sum(c.n_questions or 0 for c in design.mcqs())
    if design.mcqs():
        deltas.append(f"**Format:** essay-plus-MCQ; MCQ block(s): " + ", ".join(f"{c.id} ({c.n_questions} questions, "
                      f"{'letters only' if c.answer_format == 'letters' else 'with explanations'})" for c in design.mcqs())
                      + ". MCQ is released as one workbook with all ExamIDs on separate rows.")
    if design.exam.delivery != CANONICAL["delivery"]:
        deltas.append(f"**Delivery:** {design.exam.delivery} (canonical: in-class).")
    ceilings = [c.word_ceiling for c in design.essays()]
    if len(ceilings) != CANONICAL["n_essays"] or ceilings != CANONICAL["ceilings"]:
        deltas.append(f"**Essays:** {len(ceilings)} essay component(s), ceilings {ceilings or 'n/a'} (canonical: 2, [1200, 1000]).")
    if design.arm_ids != CANONICAL["arms"]:
        deltas.append(f"**Arms generated:** {', '.join(design.arm_ids)} (canonical: A, B). Materials per arm: "
                      + "; ".join(f"{a.id}={'none' if not a.materials else a.materials}" for a in design.arms) + ".")
    if len(design.models) != 1:
        deltas.append(f"**Models:** {len(design.models)} ({', '.join(m.model for m in design.models)}); ExamIDs are per arm and model.")
    comp_layout = f"component ids: {', '.join(c.id for c in design.components)}"
    body = [f"## {day}: {title}", "", "No deltas." if not deltas else "\n\n".join(deltas), "",
            f"Cells: {len(design.cells())} ({comp_layout}). ExamIDs: " +
            "; ".join(f"{a}{'/' + m if len(design.models) > 1 else ''}={e}" for (a, m), e in design.examid_map().items()) + "."]
    append_entry(d / "format-variations.md", _H_FORMAT, f"{design.study_id}:format", "\n".join(body))
    out.append(d / "format-variations.md")

    # prompt evolution (from the protocol's own log)
    for e in protocol.evolution:
        pbody = [f"## {e.date or day}: protocol {protocol.protocol_id} v{e.version}", "",
                 f"**Change:** {e.summary or '[HUMAN: describe the change]'} (template sha256: " +
                 ", ".join(f"{k} {v[:12]}" for k, v in e.template_sha256.items()) + ")",
                 f"**Rationale:** {e.rationale or '[HUMAN]'}", f"**Triggered by:** {e.triggered_by or '[HUMAN]'}",
                 f"**Affected exams:** {e.affected or ('this study: ' + title if e.version == protocol.prompt_version else '[HUMAN]')}"]
        append_entry(d / "prompt-evolution.md", _H_PROMPT, f"{protocol.protocol_id}:v{e.version}:{design.study_id}", "\n".join(pbody))
    out.append(d / "prompt-evolution.md")

    return out + _decision_stub(design, store, protocol, summary, audits, d, day, title, stage)


def _decision_stub(design, store, protocol, summary, audits, d, day, title, stage) -> list[Path]:
    n_cap = len(store.captured_cells())
    viol = sum(len(a.violations) for a in (audits or []))
    served = [c for c in store.captured_cells() if store.read_meta(c).get("model_fallback")]
    dl = [f"## {day}: Wave 2 {stage}, {title}", "",
          f"**Context:** Study `{design.study_id}`: {len(design.cells())} cells ({_fmt_components(design)}) x arms "
          f"{', '.join(design.arm_ids)} x models {', '.join(m.model for m in design.models)}. Protocol {protocol.protocol_id} "
          f"prompt_version {protocol.prompt_version}. [HUMAN: what prompted this run]",
          f"**Decision/event:** {n_cap} of {len(design.cells())} cells captured verbatim"
          + (f"; run status {summary.get('status')}, spend ${summary.get('spent_usd', 0):.2f}" if summary else "")
          + (f"; automated audit found {viol} violation(s) across {len(audits or [])} audited cell(s)" if audits else "")
          + (f"; {len(served)} cell(s) were served by a model other than the one requested (fallback): "
             f"{', '.join(c.id for c in served)}" if served else "") + ". [HUMAN: deviations, anomalies]",
          "**Rationale:** [HUMAN]",
          "**Protocol change (if any):** [HUMAN: none, or what changed and the prompt_version bump]",
          f"**Cross-references:** `captures/`, `audits/`, `revisions/`, `package/manifest.private.json` under `{store.root.name}/`; "
          "`prompt-evolution.md`; `materials-inventory.md`; `format-variations.md`."]
    append_entry(d / "decision-log.md", _H_DECISION, f"{design.study_id}:{stage}", "\n".join(dl))
    return [d / "decision-log.md"]


# ---- scoring ------------------------------------------------------------------------------------------------------
def write_scoring_records(design: Design, store: StudyStore, analysis: dict, rows: list[dict], *, today: date | None = None) -> list[Path]:
    from .scoring import normalize_id  # noqa: F401  (kept for symmetry)

    d = mdir(store)
    day = _today(today)
    out: list[Path] = []
    sp = d / "scoring-results.csv"
    cols, _ = _read_csv(sp)
    for r in rows:
        upsert_row(sp, SCORING_COLUMNS, r, ("exam_date", "course", "professor", "examid"))
    out.append(sp)

    # per-exam-summary scoring columns (A/B/C only: the sync script has no S column; see docs/STUDY.md)
    csvp = d / "per-exam-summary.csv"
    tot = analysis["metrics"].get("total", {"class": {}, "ai": {}})
    upd = {"exam_date": design.exam.date or "", "course": design.exam.course, "professor": design.exam.professor,
           "scoring_status": "scored" if tot.get("ai") and not analysis["missing_ai_ids"] else "partial",
           "class_n_real": str(tot["class"].get("n", "")) if tot.get("class") else ""}
    extra = []
    for e, a in tot.get("ai", {}).items():
        arm, mk = a["arm"], a["model_key"]
        label = f"{arm}" + (f"/{mk}" if len(design.models) > 1 else "")
        if arm in ("A", "B", "C") and len(design.models) == 1:
            upd[f"{arm.lower()}_total"] = f"{a['raw']:g}"
            upd[f"{arm.lower()}_rank"] = str(a["rank_real_only"])
        else:
            extra.append(f"{label} total={a['raw']:g} rank={a['rank_real_only']} z={a['z']:+.2f}" if a.get("z") is not None
                         else f"{label} total={a['raw']:g} rank={a['rank_real_only']}")
    if extra:
        _, existing = _read_csv(csvp)
        prior = next((r.get("notes", "") for r in existing if (r.get("course"), r.get("professor")) == (design.exam.course, design.exam.professor)), "")
        base = prior.split(" | scores: ")[0]
        upd["notes"] = base + " | scores: " + "; ".join(extra)
    if csvp.is_file():
        upsert_row(csvp, SUMMARY_COLUMNS, upd, ("exam_date", "course", "professor"))
        render_summary_md(csvp, d / "per-exam-summary.md")
        out.append(csvp)

    # decision-log stub
    lines = [f"## {day}: Scoring return, {design.exam.course} ({design.exam.professor})", "",
             f"**Context:** Faculty raw scores received for study `{design.study_id}`; AI ExamIDs applied only AFTER the faculty "
             f"returned raw scores (blind-grading sequence). [HUMAN: date received, who, any caveats]",
             f"**Decision/event:** real-student-only class statistics (n = {tot['class'].get('n', 'n/a')}"
             + (f", mean {tot['class']['mean']:.2f}, median {tot['class']['median']:g}, SD {tot['class']['sd']:.2f} (n-1)"
                if tot.get("class", {}).get("sd") is not None else "") + "). AI standing against real students:"]
    for e, a in sorted(tot.get("ai", {}).items(), key=lambda kv: kv[1]["raw"], reverse=True):
        lines.append(f"- ExamID {e} (arm {a['arm']}, model {a['model_key']}): {a['raw']:g}, rank {a['rank_real_only']} of "
                     f"{a['n_real'] + 1}" + (f", z {a['z']:+.2f}" if a.get("z") is not None else "")
                     + (f", percentile {a['percentile']:.0f}" if a.get("percentile") is not None else ""))
    tc = [c for c in analysis["arm_contrasts"] if c["metric"] == "total"]
    if tc:
        lines.append("Arm contrasts on the total (later minus earlier arm; rank negative = later arm ranks better):")
        for c in tc:
            lines.append(f"- {c['later_arm']} vs {c['earlier_arm']} (model {c['model_key']}): raw {c['raw_diff']:+g}"
                         + (f", z {c['z_diff']:+.2f}" if c["z_diff"] is not None else "")
                         + (f", rank {c['rank_diff']:+d}" if c["rank_diff"] is not None else ""))
    if analysis["missing_ai_ids"]:
        lines.append(f"AI ExamIDs absent from the scores file: {analysis['missing_ai_ids']}.")
    lines += ["[HUMAN: faculty AI-flag observations; the grader's verbatim qualitative comment]", "",
              "**Rationale:** per exam-taker's pre-registered analysis plan (methodology.md section 8); no new tests.",
              "**Protocol change (if any):** [HUMAN]",
              "**Cross-references:** `scoring/scoring-analysis.json`; `methodology/scoring-results.csv`; "
              "`methodology/per-exam-summary.csv`. [HUMAN: per-exam methodology scoring section; lessons-learned if warranted]"]
    append_entry(d / "decision-log.md", _H_DECISION, f"{design.study_id}:scoring", "\n".join(lines))
    out.append(d / "decision-log.md")
    return out
