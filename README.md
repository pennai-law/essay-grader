# essay-grader

AI-as-test-subject and AI-as-grader pipeline for law school exams (Penn Carey Law AI lab).

This package implements the **grader** half: a rubric-locked, dual-grader pipeline (Claude + OpenAI Decisions) that
produces *suggested* scores and a human review queue for professors grading real student essay exams.
**AI never finalizes a grade.** See [docs/GRADER.md](docs/GRADER.md) for the fairness design.

## Owner

Tulio ([VERIFY: full attribution])

## What it does

1. **Hardens** a draft rubric before any student exam is seen, in ONE budget-bounded pass (critique, adversarial synthetic answers,
   stress grading) that ends in ONE prioritized questionnaire for the professor. No back-and-forth prompting.
2. **Applies the professor's answers** (all at once) as rubric rulings, with provenance, then **locks** the rubric with a sha256 content
   hash; graders refuse unlocked or edited rubrics.
3. Optionally **calibrates** on a small anonymized sample; it emits a questionnaire in the same format, answered once and re-locked.
4. **Grades** every answer with two independent graders (Claude and OpenAI Decisions), in randomized order, with a
   deterministic length policy applied first.
5. **Flags** disagreement, unverifiable evidence quotes, and low confidence; those answers go to a human review queue.

## Workflow

```
harden  ->  answer questionnaire (once)  ->  apply-answers  ->  lock  ->  [calibrate -> answer -> apply-answers -> lock]  ->  grade  ->  human review
```

```bash
essay-grader harden --exam exam.pdf --syllabus syllabus.pdf --rubric draft.yaml --budget-usd 5 --out runs/
#   writes runs/harden-*/questionnaire.md + questionnaire.json (+ harden_report.md); read the .md, fill in answers.yaml ONCE
essay-grader apply-answers --rubric draft.yaml --questionnaire runs/harden-*/questionnaire.json --answers answers.yaml
#   writes draft.v2.yaml: rulings/descriptor rewrites applied with provenance; unanswered questions take the recommended option
essay-grader lock draft.v2.yaml                    # writes draft.v2.locked.yaml
essay-grader calibrate --rubric draft.v2.locked.yaml --sample samples/ --exam exam.pdf --budget-usd 2   # optional; questionnaire.json + a new unlocked draft
essay-grader grade --rubric draft.v2.locked.yaml --answers answers/ --exam exam.pdf --budget-usd 15 [--graders claude,openai] [--dry-run]
```

`answers.yaml` maps question id to an option label (exact, case-insensitive) or your own ruling text (`allow_custom` is always true):

```yaml
H1: "Credit the reasoned alternative"
H2: "Any reasoned conclusion earns full credit; the analysis, not the outcome, is graded."   # custom text
# H3 omitted -> the recommended option is applied and marked source=recommended_default in the rubric's provenance
```

### Budgets

Every engine entry point (`harden`, `calibrate`, `run_pipeline`) accepts `budget=Budget(max_usd=...)`; the CLI flag is `--budget-usd`.
Before each API call the worst case is estimated (input chars/4 tokens + `max_tokens` of output); if it would push spend past `max_usd`
the call is not made. After each call the actual `usage` is recorded in the budget's ledger (purpose, model, tokens, cost).

- **harden** plans the run to fit (see `harden.plan`) and degrades in this order: fewer Claude stress grades, then fewer archetypes,
  then Sonnet instead of Opus for the critique (then the questionnaire). Every degradation is listed in `harden_report.md`.
  Default `--budget-usd` is 5.
- **grade** estimates the whole run first. If it does not fit, the Claude grader is switched from Opus to Sonnet automatically (no
  prompt); if it still does not fit, `BudgetExceeded` is raised with the estimate (CLI exit code 4). Mid-run, when the budget runs out
  the run stops cleanly (exit code 5), finished answers are saved, and re-running with the same `--run-id` resumes (using the same Claude
  model as the earlier part of the run). Degradations and spend are in `run_metadata.json`.
- Without `--budget-usd` there is no cap.

### Cost reality

Prices (USD per million tokens, Anthropic pricing as of Oct 2026, in `llm.PRICE_PER_MTOK`): Opus 5.5 $4 in / $20 out / $0.20 cache read;
Sonnet 5.5 $2 / $10 / $0.20; Haiku 5.5 $0.10 / $0.50; gpt-6-luna $0.10 in. Cache writes cost about 1.25x input. Grading one answer is roughly
8k input / 1.5k output tokens: about $0.03 on Sonnet 5.5 and $0.06 on Opus 5.5, so **100 essays cost about $3-6** plus a negligible Decisions
cost. Hardening a one-question exam is roughly $1-2 at defaults (the report shows the worst-case estimate and actual spend). A **$20 per-person cap**
covers hardening plus one class-sized exam. Estimates are conservative (no prompt-cache discount); verify against billing.

`--dry-run` uses canned offline fakes for every API (no keys, no network, meaningless scores); use it to check file layout and outputs.
Re-running `grade` with the same `--run-id` resumes: finished answers are skipped, answers with grader errors are retried.
A run refuses to resume if the rubric hash, seed, or grader models changed.

Outputs go to `runs/<run_id>/`: `results.jsonl` (everything), `scores.csv` (one row per student), `review_queue.csv`,
`agreement_report.md`, `run_metadata.json` (rubric hash, pinned model ids, prompt hashes, seed, timestamps, tokens, estimated cost).

### Answer files

`answers/<pseudonymous_id>.txt|md|pdf|docx`. For multi-question rubrics name files `<id>__<question_id>.<ext>`.
The filename stem is the only identifier; use pseudonymous IDs, never names.

### Rubric format

See `examples/draft_rubric.yaml`. Criteria are `binary`, `choice` (levels with descriptors), or `score` (scale with anchors);
rubric-level `policies` cover length, off-topic material, alternative conclusions, and citations.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m pytest -q          # fully offline
```

Python 3.11+. Environment variables:

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | Claude grader, hardening, calibration (structured outputs; server-side fallback beta `server-side-fallback-2026-07-01`) |
| `OPENAI_API_KEY` | OpenAI Decisions grader (optional `OPENAI_BASE_URL`) |

Defaults: Claude `claude-opus-5-5` (override with `--model`, e.g. `claude-sonnet-5-5`; hardening uses Sonnet for bulk generation and stress grading regardless); Decisions `gpt-6-luna`.
The OpenAI Decisions adapter follows the official guide (public beta, Oct 2026; re-check at GA); see [docs/GRADER.md](docs/GRADER.md).

## Data handling rules (FERPA)

- Pseudonymous IDs only. Never put student names in filenames or file contents; the pipeline only warns on name-like headers and does not scrub.
- Never commit student data. `data/`, `runs/`, `samples/`, `answers/`, `*.pdf`, `*.docx` are git-ignored; keep it that way.
- Student text is sent to third-party APIs (Anthropic, OpenAI). Confirm the institution's agreements/zero-retention terms cover this before using real data.
- Run outputs contain student text (evidence quotes). Store them in approved storage, not in the repo.
- Only synthetic or fictional text belongs in `tests/` and `examples/`.

## Related repos

- `pennai-law/exam-tools`: generates the questions this pipeline answers and grades.
- `pennai-law/practice-sim`: the practice simulation; its design lives there and will reuse this rubric format and grading engine.
- `Box: PCL AI Project/workstreams/02-essay-grader/`: workstream home (notes, partners, drafts).
