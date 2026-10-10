# Professor Guide: Designing a Practice Simulation Assignment

For faculty. No coding. You will write the content (emails, facts, rubric, authorities list); the platform handles timing, delivery, grading support, and release.

## 1. Start with learning objectives

Pick **two or three** objectives you can see in a student's reply. Each should name a skill and a context.

- Good: "Apply UCC 2-207 to an email offer and a non-matching acceptance and say what is unresolved."
- Good: "Explain a pleading risk to a non-lawyer in under 250 words."
- Weak: "Understand contract formation." (Not observable in one email.)

Then ask: what would a competent junior associate send, and what would a weak one send? Those two replies become your rubric levels. A simulation fits best where the skill is *applying law to facts under time pressure and communicating it*, not reciting doctrine.

## 2. The arc: three emails over one week

| Day | Email | From | Purpose |
|---|---|---|---|
| Mon 9:00 a.m. | Assignment | Partner | Facts, 2-3 questions, deliverable, deadline (48-72h). |
| On submission (+15-45 min) | New facts | Client | A fact that complicates or changes the analysis. Student answers in plain English (24h). |
| Deadline + 24-48h | Feedback | Partner, after your review | Rubric results and next steps. |

Fewer than three emails feels like a quiz; more than four in a week overloads students in a 300-500 person course. Add a fourth (revision request) only for Legal Writing-type courses.

## 3. Writing persona emails

- Write in the character's real voice: short, specific, a deadline, a deliverable, a length limit.
- Include only facts students are entitled to at that stage. Put the new facts in the follow-up, not the first email.
- Partners **state tasks, not answers.** Never write "as you know, 2-207(3) applies."
- Clients are **not lawyers.** They ask what to do, what it costs, how risky it is.
- Name research expectations: "Do your own research; I cannot point you to cases."
- Use fictional names and jurisdictions; use real-law-style rules so students can find real authority.
- The platform adds the "simulated message" footer. Do not remove it.

### Template A: Partner assignment email

```
Subject: {{matter_short_name}}: {{N}} questions for {{day}}

Hi {{student.first_name}},

{{One sentence on the client and what happened.}}

Facts (as we know them): {{facts, 100-200 words, dated, neutral}}

I need a short email back from you by {{deadline, with time zone}} answering:
1. {{Open question 1}}
2. {{Open question 2}}
3. {{Optional: "If you see another issue that changes our analysis, flag it."}}

Keep it to {{limit}} words. Cite what you rely on, and tell me what you could
not resolve or what facts you would need. Do your own research; I will not be
able to point you to authorities.

{{Partner first name}}
```

### Template B: Client question / new-facts email

```
Subject: Re: {{matter_short_name}}: one more thing

Hi {{student.first_name}},

Thanks for your note. I should have mentioned: {{new fact, 1-3 sentences, in
a layperson's words}}. {{Practical worry: money, timing, relationship.}}

Does this change what you told {{partner name}}? Please tell me in plain
English what we should do next. I do not need the legal citations, just what
it means for us and how sure you are.

{{Client name}}
```

### Template C: Feedback email

```
Subject: Re: {{matter_short_name}}: my comments

Hi {{student.first_name}},

Thanks for the memo. Short version: {{one sentence overall, from reviewed
rubric result}}.

What worked:
- {{strength tied to criterion 1, with a quote from the student's reply}}
- {{strength tied to criterion 2}}

What to fix before the next one:
- {{gap tied to criterion, specific}}
- {{gap}}

Authorities I expected to see: {{expected authority list, if you choose to
share it}}.

{{Partner first name}}

--- Course feedback (from your instructor) ---
{{rubric levels per criterion; where to ask for a regrade or office hours}}
```

Feedback goes out only after a person on your team has reviewed and approved it.

## 4. Choosing deliverables

| Deliverable | Best for | Recommended limit |
|---|---|---|
| Email memo on open research questions | 1L doctrine, research | 400-500 words, 2-3 questions |
| Client advice email | Counseling, any course | 200-300 words, lay audience |
| Issue list | Civ Pro, Torts, Corporate | 300-400 words; one-sentence rule per issue |
| Clause redline + explanation | Contracts, Corporate | 1-3 clauses; 50 words per change (later feature) |
| Status update to partner | Litigation, Corporate | 150-250 words |

Short limits are a feature: they force prioritization and keep grading fast. Say whether the limit counts body only (default) or includes attachments.

## 5. Writing observable rubric criteria

The rubric uses the lab's shared format: each criterion is `binary` (yes/no), `choice` (pick a level), or `score` (a number in a range), with a descriptor for each level. A grader, human or model, should reach the same answer from the text alone.

**Bad vs good**

| Bad (subjective) | Good (observable) |
|---|---|
| "Demonstrates strong understanding of 2-207." | Binary: "States that under 2-207(1) a definite expression of acceptance operates as acceptance even if terms differ, unless acceptance is expressly conditional on assent to the different terms." |
| "Good writing." | Choice: "Opens with a one-sentence bottom-line answer (yes) / buries it after background (partial) / no answer stated (no)." |
| "Thorough research." | Binary: "Cites UCC 2-207 or Marlow's equivalent section." Binary: "Cites at least one case on the 'expressly conditional' proviso." |
| "Professional tone." | Binary: "Addressed to the partner, no slang, signs off." |
| "Correct conclusion." | Choice: "Concludes a contract formed / not formed / says it depends on X and identifies X." (Credit for reasoning, not for a single answer.) |

**Rules of thumb**

- 5-8 criteria per deliverable. Weight the one or two that matter most.
- Each level descriptor describes *what the reply contains*, not how impressive it is.
- Include an `identifies_unresolved_facts` criterion: it rewards honest uncertainty over bluffing.
- Use rubric **policies** for: over the word limit (e.g., score only the first N words, or flag), off-topic replies, and conclusions contrary to your key. For questions with defensible answers on both sides, say "credit well-reasoned contrary conclusions."
- Test the rubric: run three replies you write (strong, middling, weak) through the platform's preview and see if suggested levels match. The lab will harden the rubric adversarially, then lock it (a hash is recorded). After locking, changes make a new version.

## 6. Writing an expected-authorities list

For each research question, list:

- **Expected** (full credit): the controlling statute or rule and one or two leading cases. Give full citation, a one-line proposition, and the pin or section number.
- **Acceptable**: secondary sources (Restatement, treatise sections), alternative cases for the same point, and sections students may reach through different routes.
- **Misleading**: superseded or wrong-jurisdiction authorities, or a common wrong turn, so the rubric can mark it.

Example (abbreviated):

```yaml
- question: q1_formation
  expected:
    - cite: "UCC 2-207(1)"
      proposition: "Acceptance with additional or different terms can still form a contract unless expressly conditional."
    - cite: "Dorton v. Collins & Aikman Corp., 453 F.2d 1161 (6th Cir. 1972)"
      proposition: "'Expressly conditional' requires assent to the different terms to be made clear."
  acceptable:
    - cite: "Restatement (Second) of Contracts 59"
    - cite: "Any Marlow-equivalent annotation of 2-207"
  misleading:
    - cite: "Common-law mirror-image rule"
      note: "Displaced by 2-207 for goods; credit only if student explains why it does not govern."
```

Verify each citation yourself before launch. The system cannot confirm that cases exist or say what a student claims; it compares the student's citations to your list and flags everything else for the TA. Keep the list short; a long list invites grading by checklist.

## 7. Choosing timing

- Launch Monday 9:00 a.m. in the course time zone. State the time zone in every email.
- Deadline 48h later for 400-500 words; 72h for issue lists with research on several topics.
- Follow-up client email 15-45 minutes after the student submits (not instant).
- Feedback after the deadline, 24-48h, once TAs have reviewed. State the release date in the syllabus.
- Extensions and accommodations: tell the platform admin, not the system, who needs extra time; staff enter them. Students with extra time see their own deadline in their emails.

## 8. Pre-launch checklist

- [ ] Learning objectives (2-3) written and each tied to a rubric criterion.
- [ ] Facts fictional or authorized; names, dates, and numbers checked for consistency across all emails.
- [ ] Jurisdiction stated; rules match real law students can find (or are fully given).
- [ ] Partner email gives no legal conclusions and states word limit, deadline, time zone, and research expectation.
- [ ] Follow-up email adds exactly one material fact and is not visible before submission.
- [ ] Rubric criteria observable; policies set for length, off-topic, contrary conclusions.
- [ ] Rubric tested on strong, middling, and weak sample replies; sent for adversarial hardening; locked.
- [ ] Expected-authorities list verified by a person on Westlaw/Lexis.
- [ ] AI and collaboration policy stated in the assignment email.
- [ ] Syllabus note: student work is processed by AI services under Penn-approved terms (wording from the lab/privacy office).
- [ ] TA review plan: who reviews, how many hours, when the release is.
- [ ] Accommodations entered; extensions policy known.
- [ ] Dry run: two TAs complete the assignment as students, via both email and portal.
- [ ] Release date communicated to students.
