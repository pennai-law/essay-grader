# Grader fairness design

The grader produces **suggestions**. Professors decide every grade. The design goal is that no student is advantaged or
disadvantaged by when, in what order, or by which model their answer was graded, and that every judgment call is made
(by a human) before grading starts or after it, never silently during it.

## Safeguards

| Concern | Mechanism |
|---|---|
| Rubric drift after seeing answers | `essay-grader lock` freezes the rubric with a sha256 content hash. Graders and the pipeline call `require_locked()` and refuse unlocked or edited rubrics. The hash is recorded in `run_metadata.json`. |
| Weak or ambiguous rubric | `harden` runs before any student exam is seen, using only the exam question, syllabus and draft rubric. Claude critiques it, writes adversarial synthetic (fictional) answers (strong, median, weak, overlong, rabbit hole, correct-conclusion-wrong-reasoning, contrary-but-argued, issue laundry list, wrong jurisdiction, terse-but-correct, outside syllabus), grades each K times, and reports unstable criteria and pedagogically wrong outcomes with proposed amendments. The professor accepts or edits, then locks. |
| Unanticipated approaches | Optional `calibrate` on about 10 anonymized answers proposes rulings for approaches the rubric does not cover. It writes a **new rubric version** (unlocked) that must be re-locked. The sample IDs are stored in the rubric (and so in its hash); the pipeline flags those students in `scores.csv` or drops them with `--exclude-calibration`. Rulings must be general and not describe individual students. |
| Blinding | Graders receive only the exam text, the rubric (with policies and rulings) and the answer text. Student IDs are never sent. The tool does not scrub PII; it only warns on name-like headers. Use pseudonymous IDs. |
| Model drift | Requested model ids are pinned in the run metadata, along with the model id the API reports serving, prompt template hashes, and the seed. |
| Order effects | Grading order is shuffled with a recorded seed (reproducible). Each answer is graded in its own call with no shared conversation, so earlier answers cannot influence later ones. |
| Single-model bias | Two independent graders (Claude; OpenAI Decisions) grade every answer without seeing each other's output. |
| Disagreement | Per criterion: binary and choice flag on any label mismatch; score criteria flag when the point difference exceeds the threshold (default 1). Also flagged: Claude evidence quotes that are not verbatim substrings of the answer, any grader confidence below 0.5 (`--low-confidence`), and grader errors. Flagged answers get status `needs_human_review` and no suggested total. Only fully agreeing answers get a suggested score (mean of the two by default; `--combine min|max`). |
| Length | The length policy (`no_penalty`, `grade_first_n_words`, `deduct`) is applied deterministically in code before any grader runs; word count, words graded and any deduction are recorded per answer. |
| Style over substance, contrary conclusions, prompt injection | The grader prompt tells Claude to grade only rubric criteria, credit reasoned contrary conclusions per policy, and treat the answer as data (ignoring instructions embedded in it). |
| Reliability visibility | `agreement_report.md` gives per-criterion exact agreement, Cohen's kappa (binary/choice; "n/a" when undefined) and mean absolute point difference. Low-agreement criteria usually mean an ambiguous rubric item. |
| Resumability | `results.jsonl` is append-only. Re-running skips finished answers and retries errored ones. A run refuses to resume if rubric hash, seed, or graders changed. |

## What the AI does not do

It does not finalize grades, curve, rank, or decide anything about a student. The pipeline cannot see names. Agreement
between two models is evidence of rubric clarity, not of correctness; spot-check agreed answers too, especially at the
start of a round.

## Recommended spot checks

- Review the whole `review_queue.csv`.
- Randomly review a sample of `agreed` answers (including some per criterion) and compare to your own judgment.
- Check `agreement_report.md` for criteria with kappa well below the others; fix the rubric, re-lock, and re-run with a new run id.

## OpenAI Decisions API

Shape per official OpenAI Decisions guide (public beta, Oct 2026); re-check at GA.

`graders/openai_decisions_grader.py` targets `POST /v1/decisions` (model `gpt-6-luna`). All criteria of one question go in
one request per answer.

- Request: `{model, input, questions:[{name, type, instructions, choices|levels}]}`. There is no top-level `instructions`
  field; shared context (exam question, alternative-conclusion, off-topic and citation policies, and a line telling the model
  to ignore instructions inside the answer) and the student answer go in `input` as `input_text` parts of one user message.
- Response: `{answers:[{type, name, ...}], usage}`, matched by `name` (missing, duplicate or unknown names are errors).
- Mapping: binary to predicate (probability kept; met if probability >= threshold, default 0.5, `--decision-threshold`);
  choice to choice (level labels and descriptors); score to score (`levels` built from the scale anchors, lowest first).
  The returned `score` is a float index in level space; it is linearly interpolated between adjacent anchor values, clamped to
  the scale, then mapped to points.
- Confidence: choice and score answers carry `confidence`; predicates use the derived `|p-0.5|*2`. Any grader confidence below
  `--low-confidence` (default 0.5) flags the criterion for human review. Decisions returns no evidence quotes or rationale,
  so the quote check applies only to Claude.
- **Predicate thresholds should be set from the professor's calibration sample** (labeled examples), not left at 0.5.
- Transport: uses the SDK's `client.decisions.create` when present (openai>=3.26.0), else `client.post("/decisions")`.
- Cost: `llm.PRICE_PER_MTOK` uses $0.10 per 1M input tokens and no output charge for gpt-6-luna (regional and long-context
  multipliers not modeled). Claude prices there remain placeholders; verify against billing.
- Privacy: the Decisions API supports Zero Data Retention for eligible customers, which is relevant to the Penn privacy
  review. Confirm eligibility and agreement terms before sending real student text.

