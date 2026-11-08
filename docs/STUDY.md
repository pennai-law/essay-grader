# Study mode: the AI takes the exam

`essay_grader.study` is the "AI takes the exam" half of the engine. It is a code version of the hand-run Wave 1 (Spring 2026)
protocol in `exam-taker` (a 2 question x 2 materials design, isolated cells, verbatim capture, audit, recorded revision,
Examplify packaging, methodology record, scoring return). Wave 2 (Fall) direction from the lab meeting: lean toward
**syllabus-only** runs, **breadth** (more faculty, one model each), and an optional **three-model comparison**.

Nothing here replaces human judgment. It generates, checks and packages; people curate the record, talk to faculty, and own
IRB and the ExamID arrangement with the registrar. See [What remains human](#what-remains-human).

Source of truth for the protocol: `exam-taker/CLAUDE.md` ("Invariants"), `_shared_tooling/NEXT_SESSION_GUIDE.md`,
`Spring 2026/methodology.md` (esp. section 4.3.1 materials naivety, section 6 packaging, section 8 pre-registered analysis plan).
No code is imported from that repo at runtime; the PDF and xlsx builders are ported (see [Packaging](#packaging)).

## Workflow

```
validate -> check-materials -> run -> audit -> revise -> package -> (faculty grade blind) -> ingest-scores -> report
```

```bash
essay-grader study validate design.yaml                       # static checks, no API
essay-grader study check-materials design.yaml                # naivety scan; also runs (and gates) `run`
essay-grader study run design.yaml --budget-usd 10 --dry-run  # fake providers; outputs in runs/study/dryrun/
essay-grader study run design.yaml --budget-usd 10            # real; resumable (re-run the same command)
essay-grader study audit design.yaml                          # per-cell audit JSON
essay-grader study revise design.yaml [--llm]                 # separate, recorded second pass
essay-grader study audit design.yaml --revised
essay-grader study package design.yaml                        # PDFs (xelatex) + MCQ workbook (openpyxl)
essay-grader study ingest-scores design.yaml --scores faculty.csv [--total-column Total] [--ai-flag 0137=yes]
essay-grader study report design.yaml                         # report.md + cohort-results.draft.md
```

Mapping to `NEXT_SESSION_GUIDE.md`:

| Guide step | Here |
|---|---|
| 1 Confirm inputs, naivety, ExamIDs | `validate`, `check-materials`; `examid_policy.confirmed` gate |
| 2 Dispatch isolated cells | `run` (one API call per cell, semaphore, budget) |
| 3 Capture verbatim | `captures/<cell>.md` + `.meta.json`, read-only |
| 4 Formal-compliance audit | `audit` (`audits/<cell>.audit.json`) |
| 5 Revise on a second explicit pass | `revise` (`revisions/<cell>.revised.md`, `.diff`, `.revision.json`) |
| 6 Per-exam methodology | stubs in `methodology/` (human completes) |
| 7 Build PDFs | `package` (ported builder) |
| 8 Verify | gates inside `package` (L-043 delivered-artifact scan, section-code consistency) plus the human read-through below |
| 9 Methodology artifacts | `methodology/per-exam-summary.csv` (+ `.md`), `materials-inventory.md`, `format-variations.md`, `prompt-evolution.md`, `decision-log.md` |
| 10 Scoring return | `ingest-scores` (`scoring-results.csv`, class statistics, contrasts), `report` |

## How exam-taker's invariants are enforced

| Invariant (exam-taker CLAUDE.md) | Mechanism | Test |
|---|---|---|
| Materials boundary: each arm sees only its folder | `ArmSandbox` is the only door to materials; `..`, absolute paths, symlinks and prefix-sharing sibling folders raise `MaterialsBoundaryError`; arm folders must be disjoint; exam files and keys may not live inside any arm folder | `test_study_materials.py` |
| Subagent isolation | each cell is ONE request built from scratch from (protocol prompt, own arm's bundle); no session object exists to share | `test_study_runner.py::test_each_cell_is_one_independent_call...` |
| Verbatim capture, then audit | bytes written exactly as returned, file made read-only, sha256 recorded and re-verified on read; `revise` only writes to `revisions/` | `test_study_revise.py::test_revise_never_modifies...` |
| No fabricated authority | citation hook flags are never auto-revised; the LLM revision pass is rejected if it adds any name or number absent from the original | `test_study_revise.py` |
| Em dash ban and banned phrases | audit parses the banned list from the template's `<style>` block | `test_study_audit.py` |
| B-arm materials naive to the exam | `check-materials` blocks `run` | `test_study_materials.py` |
| L-043: no materials-condition preamble | L-043 denylist in audit (whole text), in revise (fused first sentence), in `package` (source text and, when `pdftotext` exists, the built PDF) | `test_study_audit.py`, `test_study_package.py` |
| Reserved ExamIDs, do not invent | `examid_policy.confirmed` must be true before `package`; `real_id_range` makes `validate` warn about the Wave 1 ID-range tell (L-005) | `test_study_design.py` |
| Section letter is the registrar's term code, not the arm | the arm is never an input to the PDF, filename or workbook | `test_study_package.py` |
| In-class vs take-home | `delivery: take-home` refuses to package (format not implemented; ask the operator) | `test_study_package.py` |
| Prompt-template change discipline | `Protocol.load` refuses a template whose sha256 differs from the newest evolution entry | `test_study_protocol.py` |

## Design spec (YAML)

See `tests/fixtures/study/design.yaml` (fictional). Paths are relative to the design file. Unknown keys are errors.

```yaml
study_id: fall-2026-quill-widgets
protocol: protocol/                  # directory with protocol.yaml + templates
exam: {course: ..., professor: ..., term: "Fall 2026", date: 2026-12-10, delivery: in-class}
section: A                           # registrar term-section letter, one letter, uniform across arms
components:                          # essays and MCQ blocks
  - {id: q1, kind: essay, title: "Question 1: ...", question_file: exam/q1.txt, word_ceiling: 1200}
  - {id: mcq, kind: mcq, question_file: exam/mcq.txt, n_questions: 35, answer_key_file: exam/key.txt, answer_format: letters}
arms:
  - {id: A, materials: null}                 # no materials (only A may have none)
  - {id: S, materials: materials/syllabus}   # syllabus only: the Wave 2 default
  - {id: B, materials: materials/full}       # with materials
  # per-exam extensions: {id: C, materials: materials/exam_aid}
models:
  - {key: opus, provider: anthropic, model: claude-opus-5-5, effort: high}
  - {key: gpt,  provider: openai,    model: gpt-6-luna, price_per_mtok: [0.10, 0.0, null]}   # price needed to budget-gate
examids: {"A/opus": "0137", "A/gpt": "0212", ...}   # plain "A" is allowed only with one model
examid_policy: {source: registrar-reserved, confirmed: true}
real_id_range: [1, 1203]                     # optional: lets validate warn about out-of-range or clustered dummy IDs
naivety: {ngram: 8, allow: [{arm: B, file: x.md, check: filename, reason: "..."}]}   # exceptions need a reason
package: {timestamp_base: "202612101400", line_spacing: onehalf}
```

A cell is component x arm x model and is named `<component>__<arm>__<model_key>`. ExamIDs are per (arm, model): three arms times
three models is nine reserved IDs per exam.

## Protocol assets

A `Protocol` is a directory given by path: `protocol.yaml` plus the prompt templates. **No Penn exam content belongs in this
repo**; the fixture protocol and exam under `tests/fixtures/study/` are fictional. Templates use exam-taker's two
placeholders (`[COURSE NAME]` and `[PASTE THE FULL EXAM QUESTION HERE...]`); the banned-phrase list is parsed from the line
containing "stock phrases" inside the template's `<style...>` block (so the essay and MCQ templates keep their own lists).

Discipline (mirrors "On any prompt-template change"): `protocol.yaml` records the sha256 of each template in an `evolution`
log. The newest entry must equal `prompt_version` and match the files on disk, otherwise loading fails with
`ProtocolDriftError` and prints the hash to record. To change a template: edit it, bump `prompt_version`, append an evolution
entry (summary, rationale, triggered_by, affected, hashes). Each capture records `prompt_version`, template hashes and the
rendered-prompt hash; resuming under a different prompt is refused (`StaleCaptureError`). `study run` writes
`methodology/prompt-evolution.md` entries from the log.

Wave 1 lesson L-043 is a prompt change to consider before Fall: the Wave 1 templates tell the model "Begin by noting whether
course materials are attached", which is the likely source of the B-arm preambles that disclose the condition. The fixture
protocol shows the proposed fix (an explicit instruction not to narrate files or materials) as an evolution entry.

## Materials boundary and naivety

`check-materials` scans every non-A arm's folder for: n-gram overlap with each component's question text (default 8-grams),
the exact word ceiling, the exact MCQ count (spelled-out numbers included), rubric / answer-key / exam-instructions
filenames, byte-copies of the exam or key, and (warning only) the Step 1 grep from the guide. Generic range language ("20 to 30
multiple choice questions") is in scope and only warns. Blocking findings stop `run` (exit 7) with a report naming the arm,
file, check and evidence. Exceptions need a written reason in the design and are listed in the report.

This is a mechanical screen, not a substitute for the human rule in methodology 4.3.1 (outline synthesis must never see the
exam). It cannot detect paraphrased leakage.

## Running cells

- **Anthropic**: `AnthropicTextLLM` subclasses the house `AnthropicLLM` and reuses its client, retries, usage accounting and
  budget protocol. Request: `output_config.effort` always explicit, no `tool_choice`/`temperature`/`top_p`/`top_k`/`thinking`,
  `betas=[server-side-fallback-2026-07-01]`, `fallbacks="default"`, materials in a cached system block, `stop_reason ==
  "refusal"` checked before content, truncation (`max_tokens`) returned flagged (and failed by the audit), not retried.
- **OpenAI**: `POST /v1/responses` with `model`, `input` (one user message with an `input_text` part) and `max_output_tokens`.
  The wire call is the single function `post_responses`, marked `# VERIFY against OpenAI Responses docs`, with the response
  parser `parse_responses`.
- **Third provider**: `GoogleProvider` is a stub; implement `generate(req, budget=)`, use `gated()` for budget handling, and
  `register_provider("google", factory)`.
- **Budget**: `Budget.reserve` before every call, `record` after. Models with no known price cannot be gated; with a finite
  `--budget-usd` the run refuses unless the design gives `price_per_mtok` (or `--allow-unpriced`). When the budget runs out the
  run stops cleanly (exit 5); re-run the same command to resume.
- **Resume**: a cell with capture + meta is skipped after checking prompt/materials/model/protocol still match; failed or
  refused cells keep an `.error.json` and are retried.
- **Model identity**: the requested and the served model are both recorded; a mismatch (server-side fallback) sets
  `model_fallback` in the meta, prints a warning and appears in the decision-log stub. Decide whether a fallback cell is
  acceptable before packaging.

Output layout (`runs/study/<study_id>/`, git-ignored): `prompts/`, `captures/`, `audits/`, `revisions/`, `package/deliver/`,
`package/manifest.private.json`, `package/tex_private/`, `methodology/`, `scoring/`, `report.md`, `runs.jsonl`,
`study.lock.json`. With `--dry-run` everything goes under `<out>/dryrun/`.

## Audit

One JSON per cell. Severity `violation` fails the cell; `warn` and `info` do not.

| Check | Rule |
|---|---|
| `word_count` | `wc -w` semantics; over the ceiling is a violation; outside the 85-95% band is a warning |
| `em_dash` | U+2014 anywhere (U+2013 is fine) |
| `banned_phrases` | list from the template; case-insensitive substring, like the guide's `grep -i` |
| `format` | essays: headers, bullets, numbered lines, bold, italics; MCQ letters-only: any non-answer line, answer count and order |
| `preamble` | first sentence of the answer (including the fused form) and AI/meta-commentary anywhere |
| `materials_leak` | L-043 denylist plus "no materials" tells, whole text |
| `completeness` | empty or truncated output |
| `citations` | `essay_grader.citations.check(text)` if importable (fabricated suspects fail the cell; other flags warn; "unverifiable" is informational); otherwise skipped with a note in the JSON |

Not automated: continuous-prose judgment, "no fabricated authority" beyond the citation hook, opening-sentence quality.

## Revision

Optional, separate, recorded. Never overwrites a capture. Mechanical fixes are deterministic: leading preamble sentences
matching a strict meta/leak pattern, the MCQ answer block (when commentary precedes it), em dashes (comma; en dash between
digits; or `--dash-mode en`), and banned phrases (lead-ins removed, single words via a table, extendable with
`banned_phrase_fixes` in `protocol.yaml`). Phrases with no safe fix are listed as unresolved. `--llm` adds at most one
constrained call per cell for what remains (ceiling overruns, unresolved phrases); it is logged with usage and hashes and is
rejected if it introduces a capitalized name or number absent from the original. Citation findings are never revised.
`package` builds from the revised pass when one exists and refuses any cell that still fails the audit.

## Packaging

**PDF (essays).** `package.py` ports `build_examplify_pdf.py`: same `latex_escape`, paragraph conversion, cover-page stats,
header/footer, `\setmainfont{Helvetica}`, two xelatex runs, filename `{ExamID}-{Course}_({Prof})_{Year}{Section}_{YYYYMMDDHHMM}.pdf`.
`tests/fixtures/study/golden/*.tex` were generated by running the original script, and the tests require byte-identical LaTeX
for one and two essays. **xelatex is required** to build PDFs; without it `package` writes the `.tex` sources to
`package/tex_private/`, says so, and the PDF test is skipped.

Docker (TeX Live "basic" was the Wave 1 constraint; avoid `lastpage.sty` and `tex-gyre`): install `texlive-xetex` plus the
LaTeX packages `fontspec`, `fancyhdr`, `setspace`, `geometry`, `array` (on Debian/Ubuntu `texlive-xetex texlive-latex-recommended`),
and a Helvetica-compatible font (`fonts-urw-base35`, whose Nimbus Sans is metric-compatible; check `fc-match Helvetica`).
Wave 1 ran on macOS with the system Helvetica, so confirm visually that a Linux build matches an Examplify export. Optionally
install `poppler-utils` so `package` can run the L-043 scan on the extracted PDF text.

**MCQ workbook.** One `.xlsx` per exam with every ExamID on separate rows, ported from `build_examplify_mcq_xlsx.py`:
sheet `ET_Results_<Course>_(<Prof>)_P` (31 chars max); row 1 `Exam #`, `Pts`, alternating `QN`, `QN Pts`; row 2 `ANSWER KEY -->`
with blank Pts cells (the key sentinel); student rows with an UPPERCASE letter when correct and lowercase when wrong plus
0/1 Pts; Aptos Narrow via replacement of font index 0; no freeze panes; creator and lastModifiedBy empty; filename
`MC_Results_<Course>_(<Prof>)_<Year>.xlsx`. The answer key is human-supplied; without it `package` refuses (it never
fabricates the placeholder scores described in methodology 6.2).

**No arm tells.** Filenames, LaTeX, workbook and PDF metadata never contain the arm, model or materials. Upload timestamps
follow `timestamp_base` at 3-minute steps in a seeded shuffled order, so upload order does not encode arm order. The arm to
ExamID map is `package/manifest.private.json`, outside `deliver/`.

### Known fidelity gaps (read before trusting a build)

1. **Visual format tells (L-040).** Three Wave 1 graders detected the Wave 1 AI PDFs from formatting alone (the PDFs
   were not double spaced and lacked Examplify's per-question page notations). The port reproduces the Wave 1 script exactly,
   including that gap. `package.line_spacing: double` is provided but unverified, and the per-question notations are not
   reproduced. Closing this needs a real-student Examplify export to diff against.
2. xelatex was not available where this was built: the compiled PDF has never been rendered or compared. Only the `.tex` is
   proven identical to the Wave 1 output.
3. Only 1 to 2 essay questions are supported (the verified layout). More raises `PackagingError` instead of inventing a layout.
4. One MCQ block per exam; take-home format not implemented.
5. The xlsx `docProps/app.xml` carries `Application: Microsoft Excel Compatible / Openpyxl x.y` (also true of Wave 1 files).
   `package.xlsx_application` can rewrite it; the registrar's real value is unknown.
6. The Wave 1 MCQ filename has no section code, whereas a real registrar file seen in the decision log
   (`MC_Results_..._Sec_1_2026A.xlsx`) does. Ported as-is; confirm against a Fall sample.

## Methodology record

`study run`, `package` and `ingest-scores` write into `methodology/`, in exam-taker's formats:
`per-exam-summary.csv` (columns exactly as `sync_summary_md.py` `REQUIRED_COLUMNS`; `.md` regenerated), a decision-log stub
per stage (Context / Decision/event / Rationale / Protocol change / Cross-references), a materials-inventory entry (per-arm
manifests with sizes and hashes), a format-variations entry (computed deltas against the Wave 1 canonical case, or "No
deltas."), prompt-evolution entries from the protocol log, and `scoring-results.csv`. Entries are append-only and idempotent
(content-keyed markers). Every judgment field is `[HUMAN: ...]`. Generated text has no em dashes.

Caveats: the real `scoring-results.csv` is gitignored in exam-taker, so its header is reconstructed from the guide
(`record.SCORING_COLUMNS`); if you drop the real file into `methodology/` its header is used. The per-exam summary schema has no
`s_total`/`s_rank`: arm S results go in `notes`, and the full detail is in `scoring-results.csv`.

## Scoring analysis

Input: the faculty raw-scores CSV (one row per ExamID, including the AI rows). The AI ExamIDs come from the design. Per the
sequence in CLAUDE.md, faculty send raw scores first and learn the AI IDs afterwards; run `ingest-scores` only then.

Produced (pre-registered plan, methodology section 8; nothing else): real-student-only class n, mean, median, sample SD (n-1),
min, max for the total and each component; for each AI ID the raw score, z, rank among real students (ties share the better
rank), combined rank, and percentile; arm contrasts later minus earlier (S-A, B-A, B-S): raw, z difference, rank difference
(negative = later arm ranks better), SMD for essay components, and difference in proportions plus odds ratio for MCQ; model
contrasts within each arm when there are several models; faculty AI-flag as supplied. MCQ is scored deterministically from
the final answers and the key when the faculty file has no MCQ column. Real-student rows are never written out.

Percentile is `(n_below + 0.5 n_equal) / n_real`; the plan does not define it. **Not automated:** the cross-exam random-effects
meta-analysis, I-squared and Q, Holm-Bonferroni, moderators, flag-rationale coding, the "AI passed" determination. They need
the whole cohort and human judgment; `scoring-results.csv` is their input. `cohort-results.draft.md` is a draft table for the
human-curated dashboard, not a replacement.

## What remains human

- Curating every `[HUMAN: ...]` field in the methodology record; per-exam methodology write-ups; lessons learned.
- Faculty recruitment, consent and all faculty communications (including the scoring-return sequence and survey).
- IRB posture and ethics sections; FERPA review of faculty score files (this tool keeps only aggregates).
- Getting registrar samples of every artifact type before generating, and confirming the ExamIDs.
- Providing the materials, deciding what each arm receives, and the human naivety review (especially outline synthesis).
- Reading every built artifact against a real Examplify export; deciding about served-model fallbacks.
- Answer keys, the MCQ placeholder-score contingency, and any rubric.
- Pre-registering any Wave 2 analysis changes (a decision-log entry), the headline analysis, and the paper.

## Verify live (not provable offline)

1. Anthropic text request accepted as built (`output_config` with `effort` only; whether an explicit text `format` object is
   wanted: `providers.TEXT_OUTPUT_FORMAT`), model ids, pricing, fallback beta behavior, and what `stop_reason` and
   `usage` look like for long materials.
2. OpenAI Responses shape (`post_responses`, `parse_responses`: `input` parts, `max_output_tokens`, `usage`, refusal and
   incomplete handling) and `gpt-6-luna`-style model ids and prices.
3. The `essay_grader.citations.check` result shape on real text (the audit normalizes defensively) and whether
   CourtListener lookups are desired during a run (only normalized cites leave the machine, and only with a token).
4. xelatex build in the Docker image, Helvetica availability, and a visual diff against a real Examplify export (L-040).
5. The workbook opens in Excel with the registrar's look (font, no freeze panes, properties).
6. That API cells behave like Wave 1 cells. Wave 1 cells were Claude Code subagents that read files with tools, under harness
   defaults; here the materials are inlined and there are no tool calls, so materials-narration preambles (L-043) and
   output length may differ. Dry-run a real cell per arm and read it before the full run.
7. Per-model budgets with real usage, and `--budget-usd` stop and resume on a small real run.

## Exit codes

0 ok; 1 failures (`run` with failed cells, `audit --strict`); 2 usage, design or scoring-file error; 4 budget exceeded or
unpriced model; 5 run stopped by the budget (resume); 7 materials naivety or boundary block; 8 protocol drift or error; 9 blocked
(stale or tampered capture, packaging refusal).

## Model fallback

Server-side model fallback is **off** for study cells (`AnthropicTextLLM.allow_model_fallback = False`). A cell answered by a different model than the design names would confound the model comparison, so a refusal is recorded as a data point instead. Enable it only when the design explicitly accepts substituted cells; substituted cells are still flagged `model_fallback` in the record.
