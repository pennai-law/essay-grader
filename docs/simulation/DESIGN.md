# Practice Simulation: Course-Agnostic Design

Penn Carey Law AI Lab (pennai-law). Draft, October 2026. Status: design for review; nothing here is built.

## 1. Goals and non-goals

**Goals**

- Students act as junior associates. Simulated partners and clients send timed emails; students reply with work product; the system grades replies against an instructor rubric and releases feedback on a schedule.
- Work for any course (1L Contracts, Torts, Civ Pro, Legal Writing, Corporate, Litigation, Counseling) by configuration alone. Professors author assignments in a form or YAML file, with no code.
- Handle 300-500 students per term on one VPS, without mixing student records.
- Reuse the lab's essay-grader **rubric format** and **grading engine** (Claude + OpenAI Decisions API, disagreement routed to human review). The simulation adds scenario, timing, and email layers on top; it does not fork grading.
- Instructor stays in control: auto-suggested grades are never released without review, unless the professor opts a low-stakes assignment into auto-release.

**Non-goals**

- No access to Westlaw or Lexis. Students research there; the system never proxies, scrapes, or embeds them.
- No automatic verification that cited cases exist (see section 8).
- Not a general chatbot. Personas follow scripted events; free-form persona replies are limited and guard-railed (section 9).
- Not a learning-management system. Rosters come from CSV or SSO claims; official grades are exported, not stored as the record of truth.
- No real client data. All facts, parties, and statutes are fictional or instructor-authorized.

## 2. Core concepts

| Concept | Definition |
|---|---|
| **Course** | A term-scoped class (e.g., "Contracts, Fall 2026, Section B") with instructors, TAs, roster, time zone, and default policies. |
| **Scenario** | A reusable assignment authored by staff: personas, stage graph, deliverables, rubric reference, timing rules. Mutable while in draft. |
| **ScenarioVersion** | Immutable snapshot of a Scenario, content-hashed (sha256) at publish. A run is always bound to one version, so later edits never change what a student saw or how they were graded. Edit = new version. |
| **Persona** | A simulated character (partner, client, opposing counsel) with name, role, voice notes, address, signature, and a knowledge boundary (which facts it may mention, per stage). |
| **Event / Stage graph** | Nodes are *events* (a message sent, a deadline, a feedback release). Edges are *triggers*: absolute time, relative time, on-submission, on-instructor-action. A stage is a named group of events. The graph must be acyclic and is validated at publish. |
| **Deliverable** | What the student must produce at a stage: type (email memo, client email, issue list, redline, status update), constraints (word limit, required sections, format), and the rubric it is graded on. |
| **Rubric** | The shared format used by the essay-grader: criteria with `kind` binary / choice / score, levels with descriptors, policies (length, off-topic, contrary conclusions), locked with a sha256 hash, adversarially hardened before use. Simulation adds one optional field per criterion: `evidence: expected_authorities` (section 8). |
| **StudentRun** | One student's execution of one ScenarioVersion: per-student clock offsets, accommodations, state, per-run reply token. |
| **Message** | Any email, inbound or outbound, persona or system, with timestamps, raw body, parsed body, attachments, and delivery status. |
| **Submission** | A Message (or portal form entry) accepted as the answer to a Deliverable. Immutable; resubmissions create a new Submission and the policy picks which counts (default: last on time). |
| **Feedback** | A graded result for a Submission: per-criterion level, evidence quotes, comment, status (`suggested`, `reviewed`, `released`), authorship (model, TA, instructor). |

## 3. Assignment schema (YAML)

Authors edit in a form UI that reads and writes this YAML. Validation runs at publish; the hash is stored on the ScenarioVersion. Times are expressed relative to a **launch time** set per course section (or per student for self-paced runs). Anything in `{{double braces}}` is a variable from `facts` or the student record.

### 3.1 Worked example: 1L Contracts "open questions memo"

```yaml
schema: sim/1
id: contracts-ucc-2-207-open-questions
title: "Harlan Mills: Did the Emails Make a Contract?"
course_template: contracts-1L
jurisdiction: "State of Marlow (fictional; has adopted UCC Article 2 verbatim)"
learning_objectives:
  - Apply UCC 2-207 to an email offer and a non-matching acceptance.
  - Separate what the law says from what is unresolved on these facts.
  - Report findings to a supervising attorney concisely and with authority.
timezone: America/New_York        # course default; displayed on every email
language: en

personas:
  - id: partner
    name: "Dana Whitfield"
    role: "Supervising partner, Whitfield & Pryce LLP"
    email_local: dana.whitfield      # sent from dana.whitfield@sim.<lab-domain>
    voice: "Brisk, courteous, expects short answers; never gives legal conclusions."
    knows: [facts.core, facts.stage1]
  - id: client
    name: "Marcus Oyelaran"
    role: "Owner, Oyelaran Fabrication (client)"
    email_local: marcus.oyelaran
    voice: "Non-lawyer, practical, a little impatient."
    knows: [facts.core, facts.stage1, facts.stage2]

facts:
  core: |
    Oyelaran Fabrication (Buyer) emailed Brightline Steel (Seller) on Mar 3 offering to buy
    200 tons of steel plate at $900/ton, delivery by Apr 15. On Mar 4 Brightline emailed back:
    "Accepted. Our standard terms apply, including mandatory arbitration and a 30-day warranty
    limit." Oyelaran never replied. Brightline shipped 150 tons on Apr 10; Oyelaran received
    and used it. Price of steel has since risen 30%.
  stage1: "Marlow has adopted UCC 2-207 without modification. The emails were between the two principals."
  stage2: "NEW: On Mar 5 Oyelaran's purchasing manager phoned Brightline and said 'Fine, send it.' No notes exist."

stages:
  - id: S1_assign
    at: { rel: launch, offset: "0" }          # Monday 9:00 per section launch time
    events:
      - id: e1_partner_assignment
        send: { from: partner, template: partner_assignment }
        opens_deliverable: d1
  - id: S2_followup
    events:
      - id: e2_client_followup
        trigger: { on_submission: d1 }          # fires after the student submits
        delay: { min: "15m", max: "45m", jitter: true }
        send: { from: client, template: client_new_facts }
        opens_deliverable: d2
  - id: S3_feedback
    events:
      - id: e3_feedback_d1
        trigger: { at: d1.deadline, plus: "24h" }
        action: release_feedback
        target: d1
        requires: instructor_review           # holds until a human approves

deliverables:
  - id: d1
    type: email_memo
    prompt_summary: "Short email memo on 2-3 open questions."
    due: { rel: launch, offset: "48h" }
    constraints:
      max_words: 500
      counts: body_only                     # excludes quoted text and signature
      required_sections: []                 # free form; Q-by-Q headings encouraged
      citations: { required: true, form: "Bluebook short form" }
    late_policy: { grace: "0", accept_late: true, flag: true, penalty_ref: course }
    rubric: rubrics/contracts-ucc-2-207-d1.yaml
    expected_authorities: authorities/contracts-ucc-2-207.yaml
  - id: d2
    type: client_email
    due: { rel: d1.submitted_at, offset: "24h", cap: "launch+96h" }
    constraints: { max_words: 300, audience: lay_client }
    rubric: rubrics/contracts-ucc-2-207-d2.yaml

templates:
  partner_assignment:
    subject: "Oyelaran / Brightline: three questions for Wednesday"
    body: |
      Hi {{student.first_name}},

      Our client Marcus Oyelaran has a dispute with Brightline Steel (facts below). I need
      a short email back from you by {{d1.due | datetime}} answering the open questions.

      Facts: {{facts.core}}
      Marlow has adopted UCC Article 2 without changes.

      Questions:
      1. Did the March 3-4 emails form a contract, and on whose terms?
      2. Is the arbitration clause part of it?
      3. If you spot a further issue that changes our analysis, flag it.

      Keep it to 500 words, cite what you rely on, and tell me what you could not resolve.
      Do your own research; I will not be able to point you to cases.

      Dana
  client_new_facts:
    subject: "Re: one more thing about Brightline"
    body: "Thanks for your note. I forgot: on Mar 5 my purchasing manager phoned them and said 'Fine, send it.' Does that change anything? Please answer in plain English. {{facts.stage2}}"

release:
  feedback_visible_after: { at: d1.deadline, plus: "24h" }   # never before the deadline
  mode: instructor_review                                     # or auto, for low stakes
  channels: [email, portal]

accommodations:
  allow_time_multiplier: true        # per-student, set by staff; see section 6
integrity:
  disclose_ai_personas: true
  allow_ai_assistance: "per course syllabus"   # shown on every assignment email
```

**How the pieces connect.** Event e2 fires only after submission (so a student cannot see new facts before answering d1), with a randomized 15-45 minute delay so replies feel human. Facts the client "knows" are limited per stage by `knows`. The d2 deadline is relative to submission but capped, so late starters do not run past the course window.

### 3.2 Second example: Civ Pro, personal jurisdiction (abbreviated)

```yaml
schema: sim/1
id: civpro-personal-jurisdiction-memo
title: "Kestrel Drones: Where Can We Be Sued?"
jurisdiction: "Federal court, State of Marlow (fictional) long-arm = due process limit"
personas:
  - { id: partner, name: "Priya Nandakumar", role: "Litigation partner", email_local: priya.nandakumar }
  - { id: gc,      name: "Tom Esche", role: "General counsel, Kestrel Drones (client)", email_local: tom.esche }
stages:
  - id: S1
    events:
      - { id: e1, at: { rel: launch, offset: "0" }, send: { from: partner, template: pj_assign }, opens_deliverable: d1 }
  - id: S2
    events:
      - { id: e2, trigger: { on_submission: d1 }, delay: { min: "10m", max: "30m" },
          send: { from: gc, template: pj_new_facts }, opens_deliverable: d2 }   # new fact: 40% of sales via a Marlow distributor
deliverables:
  - { id: d1, type: issue_list, due: { rel: launch, offset: "72h" },
      constraints: { max_words: 400, format: "numbered issues, one-sentence rule each" },
      rubric: rubrics/pj-d1.yaml, expected_authorities: authorities/pj.yaml }
  - { id: d2, type: client_email, due: { rel: d1.submitted_at, offset: "24h" },
      constraints: { max_words: 250, audience: lay_client }, rubric: rubrics/pj-d2.yaml }
release: { feedback_visible_after: { at: d2.deadline, plus: "48h" }, mode: instructor_review }
```

Nothing differs structurally from section 3.1 except content: personas, facts, deliverable types, and rubric. A Legal Writing course would use `type: revision_memo` with a re-submission loop; a transactional course would use `type: clause_redline`. No engine change is needed for any of them.

## 4. Email channel design

**Outbound.** Persona and system emails go through a transactional provider (Postmark, SendGrid, Amazon SES, or Resend; see the infrastructure proposal). Each email:

- From: `dana.whitfield@sim.<lab-domain>`, display name `Dana Whitfield (simulated)`.
- Reply-To: `run+<token>@sim.<lab-domain>`, where `<token>` is a random 128-bit URL-safe value unique to the StudentRun. It identifies the run and cannot be guessed.
- Footer line on every message: "This is a simulated message from a fictional character in a Penn Carey Law course exercise."
- Header `Message-ID` and `In-Reply-To` set so threads display correctly; delivery webhooks (delivered, bounced, spam) update the Message record.
- Rate limits and a per-run send cap prevent loops.

**Inbound.** The provider's inbound-parse feature posts each reply to a webhook (Postmark Inbound, SendGrid Inbound Parse, Resend inbound, or SES receiving to S3/SNS; verify support and limits for the chosen product). The webhook handler:

1. Verifies the provider signature or shared secret; rejects otherwise.
2. Resolves the token from the recipient address; unknown token is dropped and logged.
3. **Sender check.** The From address must match a verified address on the roster (Penn email) or an alias the student registered. Mismatches are held in a quarantine queue for staff, not graded.
4. Stores the raw message first, then parses: strips quoted history and signatures (provider-supplied `StrippedTextReply` where available; fallback to a library plus manual review flag), counts words on the stripped body.
5. Records `received_at` from the provider's receipt time, not the email's `Date` header (which the sender controls). Both are stored.
6. Deduplicates by Message-ID. Auto-replies (out-of-office) are detected by headers and ignored.
7. Replies "Received. Your submission to <assignment> was recorded at <time>." as a system email (not a persona), so students have an acknowledgement. Failure to parse triggers a polite "we could not read that; please resubmit through the portal" message.

**Web portal.** Authenticated (Penn SSO preferred; otherwise magic link to the roster address). It shows the inbox thread, countdown to deadline, a reply editor with live word count, and attachment upload. The portal is the **recommended primary** channel for grading-critical deliverables because it avoids parsing loss, spoofing, and spam-folder problems; email reply remains supported for realism. Both create identical Submission records. The student sees the same acknowledgement either way.

**Attachments.** Accepted types: PDF, DOCX (size cap, e.g., 10 MB, to confirm). Files are virus-scanned, stored in object storage under the run, hashed, and text-extracted for grading. Embedded images and macros are rejected. Word counts apply to the body, plus attachment text if the deliverable specifies `counts: body_and_attachment`.

**Lateness.** `received_at` is compared with the student's effective deadline (section 6). Result: `on_time`, `late` (with minutes late), or `grace`. Lateness is recorded, never silently penalized; the course policy decides the penalty and staff can waive.

## 5. State model and storage

PostgreSQL tables (names illustrative): courses, scenarios, scenario_versions, personas, student_runs, scheduled_events, messages, submissions, feedback, rubric_locks, audit_log, accommodations. All rows carry `course_id` and (where applicable) `student_run_id`; every query is scoped server-side by role. Raw emails and attachments retained per the course retention policy. Every staff action (edit, grade override, release, extension) writes an audit entry.

## 6. Timing engine

- **Scheduled jobs.** A `scheduled_events` row is created per StudentRun per event at launch, with `run_at` in UTC. A worker (BullMQ on Redis, or a Postgres-polling loop for the MVP) executes due events with idempotency keys so a restart or double fire never sends twice. The database is the source of truth; the queue is only a trigger.
- **Trigger types.** Absolute time, relative to launch, relative to a submission, relative to another event, or manual (instructor presses "release").
- **Time zones.** Store UTC; render in the course time zone (default America/New_York) and show the zone name in every email. Avoid ambiguous phrasing ("midnight Sunday"); the template renders an explicit date and time. Daylight-saving transitions are handled by computing offsets in the zone, then converting to UTC.
- **Quiet hours** (optional): persona emails are delayed until after 7:00 a.m. local if triggered overnight, unless the scenario marks them urgent.
- **Deadline extensions.** Staff can extend one student or the whole course; the change shifts `effective_deadline` and reschedules all dependent events for that run, with an audit entry.
- **Accommodations.** Staff set a per-student `time_multiplier` (e.g., 1.5x) or fixed extra time, applied at run creation to every relative deadline. The student's emails show their own deadline. Accommodation details are visible only to the registrar-authorized staff; the student record exposes only the effective times. Accommodation data is handled per Penn policy (open item).
- **Feedback release.** Never before the student's own deadline plus the configured delay; a student with extra time receives feedback after their own deadline, or the course waits until the last accommodated student finishes (professor's choice).
- **Catch-up and failure.** If the worker was down, missed events run on restart in order, with a staff alert for anything more than 15 minutes late. A daily reconciliation job compares expected and sent messages.

## 6a. Grading flow

1. **Collect.** Deadline passes (or the student submits); the Submission is frozen.
2. **Auto-suggest.** The shared grading engine runs the locked rubric: Claude and OpenAI's Decisions API (`POST /v1/decisions`; details unverified) each answer the criterion questions (predicate, choice, score). Rubric policies catch length overrun, off-topic, and contrary conclusions. Disagreement between graders on any criterion, or an unmet policy, routes the criterion to human review.
3. **Authority check.** A deterministic step compares citations extracted from the submission to the expected-authorities list and checks citation form (section 8). It is evidence for the graders, not a separate grade.
4. **Instructor/TA review.** Staff see each criterion with the suggested level, evidence quotes, and grader agreement. They accept, override (with a reason), or send for second review. Spot-check sampling is required even for agreeing criteria; the default is 10%, configurable.
5. **Release.** On the release date, approved feedback is sent as a persona email (e.g., from the partner, in voice) plus a portal page with the rubric breakdown. Grades export to CSV for the official system.

The ScenarioVersion, rubric hash, model IDs, prompts, and raw grader outputs are stored with each Feedback, so any grade can be reproduced and audited.

## 7. Feedback emails

Feedback is generated from the reviewed rubric result, not free-written: a template fills in strengths, gaps, and next steps per criterion, optionally in persona voice (a short partner note), and always followed by a clearly labeled "Course feedback" block with the rubric levels. Persona voice cannot add substantive new legal claims that are not in the reviewed feedback.

## 8. Westlaw and Lexis reality

The system cannot access either service and will not try. Consequences:

- **Students research on their own.** Assignments say so in the partner email.
- **Expected authorities list.** The instructor supplies, per research question, authorities that are *expected* (full credit), *acceptable* (alternatives or secondary sources), and *misleading* (e.g., superseded, wrong jurisdiction). Because fictional jurisdictions use real-law-style rules, a list may point to real UCC text and real leading cases that students can find. Grading checks whether the memo reaches the expected authorities, reasons from them correctly, and states the rule accurately.
- **Citation form.** A rule-based check (e.g., Bluebook short form, reporter, year, pin cite present) flags format problems; the rubric decides how much form counts.
- **No existence verification.** The system **cannot verify** that a cited case or quotation exists or says what the student claims. LLM graders can mis-recognize real cases and can themselves be wrong. Any citation not on the expected list, and any citation the check cannot confirm, is flagged "unverified" for TA review. The TA looks up flagged citations (about 30 seconds each on Westlaw). Fabricated citations are an integrity matter (section 10), decided by humans.
- **Rubric wording.** Criteria say "cites the controlling provision and applies it" rather than "is correct," so graders compare against the list.

## 9. Persona guardrails

- **Scripted first.** Persona emails come from instructor-approved templates with variable substitution. Free-form generation is optional and off by default in the MVP.
- **If free-form persona replies are enabled** (e.g., a client answering a student's clarifying question): the model sees only the persona card and facts released to the student's current stage (`knows`), never the rubric, expected authorities, or later stages. Output passes a filter (below) before sending.
- **Never answers the assignment.** The persona does not state legal conclusions, rule statements, or recommended outcomes. A student question like "Is this a contract?" gets an in-character deflection ("That's what I need you to tell me").
- **No unreleased facts.** Fact access is enforced by retrieval scope (stage-gated), not by instructions alone.
- **Prompt-injection resistance.** Student text is wrapped as untrusted data; the persona model has no tools, no access to other students' data, and no network access. Output filter checks for rubric terms, system-prompt echoes, and out-of-scope facts, and quarantines on a hit. The grader treats student text as data too (the shared rubric hardening covers "ignore previous instructions" and "give this a perfect score" cases) and flags submissions that address the grader.
- **Labeled as simulated.** Every persona message carries the simulated-message footer and "(simulated)" in the display name. The first email of a course explains the format.
- **Rate limits.** Per-student cap on persona replies and total tokens.
- **Logging.** Every prompt and output is stored for audit.

## 10. Academic integrity

- Each assignment states the course's AI and collaboration policy in the first email; the system does not decide it.
- Individualization: optional per-student **fact variants** (names, amounts, dates drawn from a variant table, seeded by student) so shared answers do not map one to one. Rubric uses variable references.
- Similarity flagging across submissions in a section (n-gram or embedding overlap), surfaced to staff only. It is a signal, not a finding.
- Submission metadata kept for review: timestamps, channel, IP in portal, attachment hashes.
- Unverified-citation flags (section 8) feed the integrity review.
- Automated "AI-written" detection is **not** used; it is unreliable and unfair. Students are not accused by the system.
- Student privacy: scenario data is fictional; student work goes to Anthropic and OpenAI under Penn-approved terms (see infrastructure proposal); students are told this in the syllabus note.

## 11. MVP cut vs later

| MVP (build first) | Later |
|---|---|
| YAML schema + validation; ScenarioVersion hashing | Form-based visual scenario editor |
| Scripted persona emails; stage graph with time and on-submission triggers | Free-form persona chat with guardrail filter |
| Deliverables: email memo, client email, issue list | Clause redline with DOCX diff, branching events |
| Portal submission (primary) + inbound email reply parsing | Calendar invites, SMS nudges, group runs |
| Shared rubric + grading engine, staff review UI, release by instructor | Auto-release option, regrade requests, analytics across sections |
| Per-student time multiplier, instructor extensions | Self-paced cohorts, scenario marketplace between professors |
| Expected-authorities comparison + citation-form check + TA flag list | Citation existence checking via licensed data source (needs vendor agreement) |
| Single-course pilot (one section, 30-60 students), then 300-500 | Roster sync with Penn systems |

**Infrastructure reuse (candidate, not confirmed).** The tjtags/counselview repository (Next.js, Prisma/PostgreSQL, BullMQ/Redis worker, Nodemailer SMTP, Docker Compose with Caddy, AuditLog table, field encryption helper) contains patterns that could be reused: a separate worker process with run/audit records (`agents/worker.ts`), an audit-log table (`prisma/schema.prisma`, `src/lib/audit.ts`), an encryption helper (`src/lib/encryption.ts`), and Compose plus Caddy deployment files. It is a law-firm practice-management app, not a simulation platform, and it sends mail through direct SMTP; an inbound-reply pipeline and scenario engine would be new. The Matter Zero code described in the handoff was not located, so no reuse from it is assumed. Reuse should be by copying small patterns after review, not by forking the app.
