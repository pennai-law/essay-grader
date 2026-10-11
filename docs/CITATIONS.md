# Citation extraction and verification

Students research on Westlaw/Lexis, which this tool cannot access. Both the essay exams and the AI exam-taking study forbid
fabricated authority, so the engine extracts citations and checks that they exist in **CourtListener** (Free Law Project's
free case-law database). Like everything here it produces **flags for a human**, never a finding or a grade change.

```bash
essay-grader citations check memo.txt [--expected expected.yaml] [--out report.md] [--fail-on-suspect]
essay-grader grade ... --check-citations [--expected-authorities expected.yaml]
```
```python
from essay_grader.citations import check
report = check(text)            # stable public function; report.to_markdown(), report.to_dict(), report.fabricated_suspects
```

## What is and is not verified

| Item | Handling |
|---|---|
| Case cites (volume reporter page, incl. parallel cites) | Extracted with `eyecite` (regex fallback), looked up in CourtListener: found / not found / ambiguous / invalid reporter |
| Short forms, `id.`, `supra` | Extracted and resolved to the full cite they refer to; not looked up themselves. Short-form pin cites are range-checked |
| Statutes and regulations (U.S.C., C.F.R., state codes) | **Extraction only**, status `not_verified_statute`, flag `unverifiable`. No free verifier is wired in |
| Quotations, propositions, holdings | **Never checked.** A real case can be cited for something it does not say |
| Secondary sources, law reviews, restatements | Not checked |

Per case cite: existence; **name consistency** (the name written beside the cite vs the matched case name; fuzzy, abbreviation-aware,
threshold 0.6, compares each party so "Brown v. Board" matches); **year consistency** (exact; the year is parsed from the parenthetical
after the cite); **pin plausibility** (pin page >= first page and within 250 pages).

Flags: `fabricated_suspect` (not found, or reporter not recognized), `name_mismatch`, `year_mismatch`, `unverifiable` (no token, service
error, or statute), plus informational `pin_implausible`. Ambiguous matches (several clusters) are not flagged as fabricated.

## False-positive risks (a flag is not proof)

- **Unreported or unpublished opinions** and slip opinions (`2023 WL ...`, `LEXIS`): not in the reporter-cite index, so they are not
  extracted as case cites; one with an invented reporter-style cite is reported as not found.
- **Very recent cases**: CourtListener lags; a real decision from the last weeks or months may be missing. `not_found` cache entries expire after 7 days for this reason.
- **State and regional reporters, older and local courts**: coverage is uneven. Absence is weaker evidence outside federal/SCOTUS.
- **Typos** (a transposed digit) look the same as invention: `not_found` or a name mismatch. Read the matched case before concluding.
- **Year**: decision year and reporter-volume year can differ by one; the check is exact, so expect an occasional harmless `year_mismatch`.
- **Name check**: unusual short names, "In re" captions, and government parties ("United States v. X") can score low.
- **eyecite mis-parses** are possible, especially for odd formats. eyecite attaches a later cite's year to an earlier cite when several share a sentence, so years and pins are parsed by this module instead.
- **False negatives**: a fabricated case with a *real* cite (someone else's) is caught only by the name/year checks.

## Setup

1. Create a free CourtListener account and copy the API token (Profile > API).
2. Provide it as an environment secret, never in a file or the repo: `export COURTLISTENER_API_TOKEN=...` (in a Claude Code cloud environment, add it under the environment's secrets).
3. Without a token nothing is sent anywhere and every case cite is `unverifiable`; the report says so at the top.

**Rate limits and cost**: cites are batched into one request per up to 100 cites; HTTP 429 / 5xx / transport errors retry with exponential
backoff (honoring `Retry-After`), then the cite is `unverifiable`, never `fabricated_suspect`. Results are cached in sqlite
(`~/.cache/essay_grader/citations.sqlite`, or `$ESSAY_GRADER_CACHE`; found 90 days, not-found 7 days, errors never). The free API is throttled
(about 60 valid cites per minute at last check), so big classes take minutes; the cache makes re-runs instant.

**Privacy (FERPA)**: only normalized cites such as `347 U.S. 483; 410 U.S. 113` are sent to CourtListener, never the student's text.
Cites still reveal what an anonymous answer cited; keep pseudonymous IDs as elsewhere.

**Unverified adapter**: the request and response shapes in `citations/courtlistener.py::_lookup_batch` follow the v4 `citation-lookup` endpoint
from memory (the sandbox cannot reach courtlistener.com) and are marked `# VERIFY against CourtListener citation-lookup docs`. Check the
field names, the per-cite status codes (200/300/400/404/429) and the batch limits against the live docs and one real call before relying on it.

## Pipeline hook (off by default)

`GradeConfig.check_citations=True` (CLI `--check-citations`) checks each answer's full text and writes `citations` into `results.jsonl`. A
`fabricated_suspect` adds the review reason `citation:fabricated_suspect` and sets the status to `needs_human_review` (so it appears in
`review_queue.csv`). **Points, criteria, and `suggested_total` are never touched**; note that this differs from other review reasons, where no
total is suggested, because the professor should still see the score the graders reached. If the checker raises, grading continues and the record carries `citations_error`.

## Expected authorities

`expected.yaml` (list or `{authorities: [...]}`) of names, cites, or `{name, cite, aliases}`:

```yaml
- Brown v. Board of Education, 347 U.S. 483
- name: Roe v. Wade
  cite: 410 U.S. 113
- 42 U.S.C. § 1983
```
The report lists **cited** (matched by reporter cite, fuzzy case name, or a bare mention of the name with no cite), **missed**, and **extra**
authorities the answer relied on that are not on the list. Extra is a prompt to look, not a penalty: unlisted authority may be right on point.

## Interactive use

The org's Claude connectors include a **CourtListener** connector. For interactive, one-off checks (a single disputed cite, reading the
actual opinion to test a quoted proposition) use that connector in a Claude session; this module is the batch, offline-testable path.
