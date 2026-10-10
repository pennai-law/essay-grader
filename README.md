# essay-grader

AI-as-test-subject and AI-as-grader pipeline for law school exams (Penn Carey Law AI lab).

This package implements the **grader** half: a rubric-locked, dual-grader pipeline (Claude + OpenAI Decisions) that
produces *suggested* scores and a human review queue for professors grading real student essay exams.
**AI never finalizes a grade.** See [docs/GRADER.md](docs/GRADER.md) for the fairness design.

## Owner

Tulio ([VERIFY: full attribution])

## What it does

1. **Hardens** a draft rubric before any student exam is seen (critique, adversarial synthetic answers, repeated stress grading).
2. **Locks** the rubric with a sha256 content hash; graders refuse unlocked or edited rubrics.
3. Optionally **calibrates** on a small anonymized sample, producing a new rubric version that must be re-locked.
4. **Grades** every answer with two independent graders (Claude and OpenAI Decisions), in randomized order, with a
   deterministic length policy applied first.
5. **Flags** disagreement, unverifiable evidence quotes, and low confidence; those answers go to a human review queue.

## Workflow

```
harden  ->  (professor edits draft)  ->  lock  ->  [calibrate  ->  (edit)  ->  lock]  ->  grade  ->  human review
```

```bash
essay-grader harden --exam exam.pdf --syllabus syllabus.pdf --rubric draft.yaml --out runs/
#   read runs/harden-*/harden_report.md, edit draft.yaml
essay-grader lock draft.yaml                       # writes draft.locked.yaml (rubric.yaml -> rubric.locked.yaml)
essay-grader calibrate --rubric rubric.locked.yaml --sample samples/ --exam exam.pdf   # optional; writes a NEW unlocked version
essay-grader lock runs/calibration/rubric.v2.yaml  # re-lock before grading
essay-grader grade --rubric rubric.locked.yaml --answers answers/ --exam exam.pdf [--graders claude,openai] [--dry-run]
```

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
| `ANTHROPIC_API_KEY` | Claude grader, hardening, calibration |
| `OPENAI_API_KEY` | OpenAI Decisions grader (optional `OPENAI_BASE_URL`) |

Defaults: Claude `claude-opus-5-5` (override with `--model`, e.g. `claude-sonnet-5-5`); Decisions `gpt-6-luna`.
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
