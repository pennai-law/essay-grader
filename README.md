# essay-grader

AI-as-test-subject and AI-as-grader pipeline for law school exams.

## Owner

Tulio ([VERIFY: full attribution])

## Status

🚧 **Stub.** v1 pipeline exists in Polk's personal workspace (`~/code/exam-grader`, `~/code/essay-dry-run`). This repo will be the consolidated, lab-owned version.

## What this is

A unified pipeline that:

1. **Takes exams as a subject.** Feeds law school exam questions to AI models (Claude primarily) under controlled conditions. Captures answers, timing, and meta-information.
2. **Grades essays as a grader.** Applies rubrics to student or AI-generated answers. Produces structured scores, feedback, and reliability metrics.

The two halves are designed to compose: AI takes the exam, the same pipeline grades the resulting answers, we compare against human grading on the same materials.

## What's planned (Summer 2026)

- Consolidate v1 code from `~/code/exam-grader` and `~/code/essay-dry-run` into this repo.
- Write up the v1 pipeline: architecture, prompts, results to date.
- Plan and start v2 (wave 2 partner schools).
- Develop a faculty survey instrument for participating professors.

## Related repos

- `pennai-law/exam-tools` — generates the questions this pipeline answers and grades.
- `Box: AI Teaching Lab/workstreams/02-essay-grader/` — workstream home (notes, partners, drafts).

## Setup

(To be filled in when v1 lands here.)
