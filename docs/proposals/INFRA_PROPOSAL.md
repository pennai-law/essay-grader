---
title: "Database and Email Infrastructure Proposal"
subtitle: "Penn Carey Law AI Lab: Essay Grader and Practice Simulation"
author: "Prepared by Tulio, for Polk"
date: "October 2026"
---

# 1. Summary

The lab is building two tools: an **essay grader** (reads student exams and grades them against a rubric) and a **practice simulation** (students act as junior associates and exchange timed emails with simulated partners and clients). They need different infrastructure.

- The grader can run as a **batch job**: start it, grade a few hundred exams, stop. It needs a database and AI access, not a public server.
- The simulation needs an **always-on server** that sends scheduled emails, receives replies, and shows students a web page.

**Recommendation:** one small rented server (VPS) running both, with off-site encrypted backups, a transactional email service, a lab-owned domain, and shared lab accounts. Estimated infrastructure cost: roughly **$50-$150 per month** before AI usage (approximate, verify before purchase). The larger cost and the main risk is not hardware: it is **approvals** for sending student work to AI vendors.

# 2. What is needed and why

| Need | Grader | Simulation | Why |
|---|---|---|---|
| Server (VPS) | Optional; can run on a laptop or the VPS | Required, always on | Emails must send on schedule at any hour; students need a website. |
| Database (Postgres) | Yes | Yes | Stores rubrics, submissions, grades, audit trail. |
| Job queue (Redis) | No | Yes | Fires timed events and retries failures. |
| Email service | No | Yes | Delivers persona emails and receives student replies. |
| Domain | No | Yes | Email deliverability needs a domain we control. |
| AI API access | Yes (Anthropic and OpenAI) | Yes | Grading; limited persona text. |
| Backups | Yes | Yes | Grades and student work must be recoverable. |

# 3. Recommended stack

**One VPS running Docker Compose** with five containers: the web app, a background worker, PostgreSQL, Redis, and Caddy (web server that handles HTTPS certificates automatically). This is a standard, widely used pattern. Some of it can be adapted from an existing internal project (counselview) that already uses Compose, Postgres, Redis, and Caddy; this is a candidate only, to be reviewed before reuse.

**Server options** (approximate monthly prices; verify before purchase):

| Option | Spec (4 vCPU, 8 GB RAM, 80+ GB disk) | Approx. price |
|---|---|---|
| Hetzner Cloud | Shared vCPU instance (EU or US regions) | $8-$25 |
| DigitalOcean Droplet | General-purpose | $40-$65 |
| Linode (Akamai) | Shared CPU plan | $30-$50 |

Hetzner is cheapest; DigitalOcean and Linode have US regions and more familiar billing. Region and vendor matter for the privacy review (section 6). A 300-500 student course is a light load for this size; the demanding moments are deadlines, when many replies arrive within minutes, which a queue handles.

**Managed Postgres alternative** (DigitalOcean, Linode, or similar): about $15-$60 per month. It adds automatic backups and failover and removes database maintenance from Tulio, at higher cost. Recommended if the lab wants the service to outlive one person's attention; the VPS with self-managed Postgres is acceptable for the pilot.

**Backups:** nightly encrypted database dumps to an off-site object storage bucket (Backblaze B2, Amazon S3, DigitalOcean Spaces, or Hetzner Object Storage), approximately $1-$10 per month. Restore is tested before launch, not only configured.

**Email service** (approximate; verify before purchase):

| Provider | Outbound | Inbound reply parsing | Approx. price at lab volume |
|---|---|---|---|
| Postmark | Strong deliverability | Yes, inbound webhooks (confirm plan) | $15-$50 |
| SendGrid | Yes | Yes, Inbound Parse | $20-$90 |
| Amazon SES | Yes, very low cost | Yes, via S3/SNS (more setup) | $1-$10 |
| Resend | Yes, developer-friendly | Inbound support must be confirmed | $20-$40 |

Expected volume is small (about 1,500-5,000 emails per assignment cycle). Recommended: **Postmark** for simplicity and deliverability, or **SES** for lowest cost. Inbound parsing support and plan limits must be confirmed with the chosen vendor. The web portal is the fallback so grading never depends on parsing.

**Domain:** a lab-owned domain or a Penn-approved subdomain (for example sim.<lab-domain>), with email authentication records (SPF, DKIM, DMARC). Using Penn DNS may require a request to Penn IT; a lab-owned domain is quicker. Approximately $12-$20 per year if purchased.

**Shared accounts:** per your guidance of widely adopted tools on shared accounts, all vendor accounts (VPS, email, object storage, domain, AI vendors) will be registered to a **shared lab address** (not a personal one), with two or more administrators, multi-factor authentication, and a written handover note. This also protects against any one person leaving.

**Secrets management:** API keys and passwords are kept out of code, stored in the server's protected environment file and in the lab's shared password manager, rotated each term, and never emailed. Production and development keys are separate.

# 4. AI API costs

Cost per student is estimated as **tokens x price**. I have not fixed prices; they must be taken from each vendor's current price list.

**Method**

1. Tokens per grading call = rubric + instructions (input) + student submission (input) + graded output.
2. Cost per call = (input tokens x input price) + (output tokens x output price).
3. Multiply by the number of graders (two: Claude and OpenAI), the number of criteria calls, and retries.
4. Cost per student = sum across deliverables; total = per student x enrollment.

**Illustrative assumptions (not quotes):** a 500-word memo is about 700 tokens; rubric and instructions about 3,000 tokens; output about 1,000 tokens; 2 vendors; 2 deliverables per student; 1.5x allowance for retries and re-grades. That is roughly 4,700 tokens in and 1,000 out per call, about 19,000 tokens per student across both vendors, deliverables, and retries. At a placeholder rate of $3 per million input and $15 per million output tokens, the result is on the order of **a few cents to about $0.20 per student**, or **under $100 for 500 students**. Essay exams are longer (3,000-8,000 words), which can raise the figure by 10 times, still likely **under a few hundred dollars per round** at these assumptions. OpenAI's Decisions API (gpt-6-luna, public beta) is far cheaper: $0.10 per million input tokens and no output charge, so the OpenAI half of grading is a rounding error (well under $1 for 500 students); it also supports Zero Data Retention for eligible accounts. Claude rates above remain placeholders to confirm.

**Budget caps:** a monthly spending cap set in each vendor console (suggest $300 initially), per-student and per-run token limits in the software, alert emails at 50% and 80%, and a kill switch to disable AI calls. Review the first round's actual cost before the next.

# 5. Privacy and compliance

Real student exams will be sent to **Anthropic and OpenAI**. This cannot start until the following are done:

- **Penn approval:** review by Penn's Information Security and Privacy office (ISC and privacy office) of the data flow, with vendor data-processing terms. Ask for **zero-data-retention or no-training terms** where the vendors offer them, and confirm whether Penn already has an enterprise agreement with either vendor that can be used.
- **IRB sign-off** if any research use of student work is intended (on Polk's list). Grading for instruction alone may be treated differently; the IRB decides.
- **FERPA:** student exams are education records. Whether the vendors qualify as "school officials" or require other arrangements is for **Penn to determine**; this proposal does not assume an answer.
- **Student notice:** syllabus language that AI services assist with grading and that humans review all grades.
- **Data minimization:** grade pseudonymous IDs (not names); strip identifying information before sending; retain only per the course retention policy; encrypt backups; limit staff access; audit logging.
- **Simulation data** is fictional except student replies and accommodation data, which stay on lab-controlled storage; accommodation details are limited to authorized staff.
- **Hosting location** (US region) and vendor choice will be confirmed with the privacy office before real data is loaded.

Until approval, development and testing use **synthetic or consented sample exams** only.

# 6. Timeline: first grading round, end of October

Today is October 10, 2026. The end-of-October date depends mainly on approvals, which are outside our control.

| Week | Dates | Work |
|---|---|---|
| 1 | Oct 10-16 | Director decisions (below). Open shared accounts. Submit Penn privacy/ISC and IRB requests. Provision VPS, domain, backups. Grader finishes rubric format and lock. |
| 2 | Oct 17-23 | Adversarial hardening of the rubric with Anthropic. Grader dry run on synthetic exams, with disagreement review. Restore test of backups. Email service and domain verified. |
| 3 | Oct 24-30 | Approvals confirmed in writing, then a first real grading round (batch). Human review of all flagged disagreements and a sample of agreements. |
| After | Nov+ | Review cost and accuracy. Simulation pilot (one section) in November; full 300-500 students in the next term. |

If approvals are not in hand by October 24, the round runs on synthetic exams and the real round slips; it does not run on real data without approval.

# 7. Decisions requested from the director

- Approve the single-VPS plan, and choose between self-managed Postgres and managed Postgres.
- Choose the hosting vendor (Hetzner, DigitalOcean, or Linode), noting region and privacy review.
- Approve an email vendor (Postmark recommended; SES for lowest cost).
- Approve a lab-owned domain, or request a Penn subdomain.
- Name the shared lab email for accounts, and two named administrators.
- Approve an initial monthly spending cap for infrastructure (suggest $150) and AI usage (suggest $300).
- Confirm who submits and follows up on the Penn privacy/ISC and IRB requests, and whether Penn has existing vendor agreements.
- Decide whether the first round proceeds on synthetic data if approvals slip.
- Name the staff reviewer for grading disagreements and TA support for the simulation.

*All prices are approximate, verify before purchase.*
