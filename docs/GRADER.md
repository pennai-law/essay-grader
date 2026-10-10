# Grader fairness design

The grader produces **suggestions**. Professors decide every grade. The design goal is that no student is advantaged or
disadvantaged by when, in what order, or by which model their answer was graded, and that every judgment call is made
(by a human) before grading starts or after it, never silently during it.

## Safeguards

| Concern | Mechanism |
|---|---|
| Rubric drift after seeing answers | `essay-grader lock` freezes the rubric with a sha256 content hash. Graders and the pipeline call `require_locked()` and refuse unlocked or edited rubrics. The hash is recorded in `run_metadata.json`. |
| Weak or ambiguous rubric | `harden` runs once, before any student exam is seen, within a dollar budget, using only the exam question, syllabus and draft rubric. One Opus critique; ONE Sonnet call writes all adversarial synthetic (fictional) answers (strong, median, weak, overlong, rabbit hole, correct-conclusion-wrong-reasoning, contrary-but-argued, issue laundry list, wrong jurisdiction, terse-but-correct, outside syllabus); each is graded once by Claude (evidence quotes) and K=5 times by OpenAI Decisions (variance). A final call merges critique findings, score spreads and pedagogically wrong outcomes into ONE questionnaire of at most 12 questions ranked by impact. The professor answers it once; `apply-answers` writes the rulings into a new draft with provenance; then lock. |
| Unanticipated approaches | Optional `calibrate` on about 10 anonymized answers emits a questionnaire (same format as hardening) about approaches the rubric does not cover, plus a **new rubric version** (unlocked, no rulings yet). Answers are applied with `apply-answers` and the result must be re-locked. The sample IDs are stored in the rubric (and so in its hash); the pipeline flags those students in `scores.csv` or drops them with `--exclude-calibration`. Rulings must be general and not describe individual students. |
| Blinding | Graders receive only the exam text, the rubric (with policies and rulings) and the answer text. Student IDs are never sent. The tool does not scrub PII; it only warns on name-like headers. Use pseudonymous IDs. |
| Model drift | Requested model ids are pinned in the run metadata, along with the model id the API reports serving, prompt template hashes, and the seed. |
| Order effects | Grading order is shuffled with a recorded seed (reproducible). Each answer is graded in its own call with no shared conversation, so earlier answers cannot influence later ones. |
| Single-model bias | Two independent graders (Claude; OpenAI Decisions) grade every answer without seeing each other's output. |
| Disagreement | Per criterion: binary and choice flag on any label mismatch; score criteria flag when the point difference exceeds the threshold (default 1). Also flagged: Claude evidence quotes that are not verbatim substrings of the answer, any grader confidence below 0.5 (`--low-confidence`), and grader errors. Flagged answers get status `needs_human_review` and no suggested total. Only fully agreeing answers get a suggested score (mean of the two by default; `--combine min|max`). |
| Length | The length policy (`no_penalty`, `grade_first_n_words`, `deduct`) is applied deterministically in code before any grader runs; word count, words graded and any deduction are recorded per answer. |
| Style over substance, contrary conclusions, prompt injection | The grader prompt tells Claude to grade only rubric criteria, credit reasoned contrary conclusions per policy, and treat the answer as data (ignoring instructions embedded in it). |
| Reliability visibility | `agreement_report.md` gives per-criterion exact agreement, Cohen's kappa (binary/choice; "n/a" when undefined) and mean absolute point difference. Low-agreement criteria usually mean an ambiguous rubric item. |
| Resumability | `results.jsonl` is append-only. Re-running skips finished answers and retries errored ones. A run refuses to resume if rubric hash, seed, or the requested graders changed; a resumed run keeps the Claude model it was (auto-)downgraded to, so one run uses one model per grader. |
| Cost overruns | A `Budget` gates every API call (see below). |

## One-shot hardening and the questionnaire

Hardening never asks the professor anything while it runs. Output is `questionnaire.json` (+ readable `questionnaire.md`):

- At most `--max-questions` (default 12) questions, ranked by impact = criterion weight (criterion points / total points; 1.0 for rubric-level
  policy) x observed spread (max of the criterion's point spread across stress grades as a fraction of its max, and the cited answer's distance
  outside its expected score band; floor 0.1) x severity (1-5, from the model). Near-duplicates (same criterion, similar text) are merged.
- Each question: `id` (H1.., C1.. for calibration), `criterion_id` (null = rubric-level policy), a concrete `scenario` and a verbatim `excerpt`
  from the triggering synthetic answer (replaced by a real slice of the answer if the model's excerpt is not verbatim), `why_it_matters`, 2-4
  `options` (`label` plus exactly one of `ruling` text or a descriptor `rewrite {target, text}`; neither = "keep as written"), `recommended`,
  `allow_custom: true`.
- `apply_answers(rubric, questionnaire, answers)` writes each choice into a new draft: rulings go to the criterion's `edge_case_rulings` (or
  `policies.additional_rulings`), rewrites replace the description / level or anchor descriptor / a policy. Each application is recorded in
  `rubric.provenance` (`hardening_run_id`, `question_id`, option, previous text, `source` = `answer` | `custom` | `recommended_default`) and the
  version is bumped. Provenance is part of the rubric content hash, so the audit trail is frozen by `lock`. A questionnaire only applies to the
  draft it was generated for (hash check; `--allow-stale` overrides).
- If the budget or a refusal prevents the questionnaire call, a mechanical questionnaire is built from the critique findings (adopt amendment /
  keep as written) and the degradation is recorded.

## Budget semantics

`Budget(max_usd, spent_usd, ledger)`; `remaining()`. Per call: worst-case estimate = input chars/4 tokens x input price + `max_tokens` x output
price; in-flight estimates are reserved so concurrent calls cannot jointly overshoot; actual cost comes from `response.usage` (`input_tokens`,
`output_tokens`, `cache_creation_input_tokens` at 1.25x, `cache_read_input_tokens`). Unpriced models cost 0 and are not gated. OpenAI Decisions
usage is recorded but not gated (about $0.10 per million input tokens).

- Hardening plan (`harden.plan`): defaults are the Opus critique, one Sonnet call for all synthetic answers, one Sonnet stress grade per answer +
  5 Decisions repeats, one Opus questionnaire. Sized to the exam (11 archetypes for a one-question exam, 8 for multi-question, many-criteria or
  long exams). Degradation order: fewer Claude stress grades (to 3 per question), fewer archetypes (to 4), Sonnet critique, Sonnet questionnaire,
  no Claude stress grades; if the minimum still does not fit, `BudgetExceeded`. Recorded in `harden_report.md` and the report JSON.
- Grading: pre-run estimate (typical 1.5k output tokens) of all pending answers; Opus to Sonnet automatic downgrade, then `BudgetExceeded`;
  mid-run, each answer is gated on its worst case, and the run stops cleanly and can be resumed with the same run id.

Rough per-answer cost: about 8k input / 1.5k output tokens, i.e. about $0.03 (Sonnet 5.5) or $0.06 (Opus 5.5); 100 essays are about $3-6 plus a
negligible Decisions cost. A $20 per-person cap covers hardening plus one class-sized exam.

## Claude API usage

`llm.AnthropicLLM` follows the current Anthropic docs for the 5.5 models: structured output via
`output_config={"format": {"type": "json_schema", ...}}` (parsed and validated with pydantic; no forced `tool_choice`, which returns HTTP 400),
no `temperature`/`top_p`/`top_k`/`thinking`, and `output_config.effort` always explicit (grading, critique and questionnaire `high`; synthetic
answers `medium`). Requests go through `client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`.
`stop_reason == "refusal"` is checked before content is read; a refusal yields a structured error result (`CallResult.error`/`refused`), which the
grader reports as a grader error (answer goes to human review) and hardening records; no grade is ever fabricated. `messages.parse` exists in
the installed SDK but is not used because it parses the text before the caller can inspect `stop_reason`.

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
  multipliers not modeled); Claude prices are Anthropic pricing as of Oct 2026. Verify against billing.
- Privacy: the Decisions API supports Zero Data Retention for eligible customers, which is relevant to the Penn privacy
  review. Confirm eligibility and agreement terms before sending real student text.

