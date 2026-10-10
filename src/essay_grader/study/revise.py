"""Revision pass: optional, recorded separately, never touches the verbatim capture.

Output per revised cell (only when something changed):
    revisions/<cell>.revised.md      the revised text
    revisions/<cell>.diff            unified diff, capture -> revised
    revisions/<cell>.revision.json   reason, every step, unresolved items, LLM call log, post-revision audit

1. Mechanical fixes, deterministic (same input, same output):
   - leading preamble sentences that match a STRICT meta/leak pattern (the L-043 fused-first-sentence case);
   - MCQ: keep only the final contiguous 1..N answer block when commentary precedes it;
   - U+2014: between digits becomes an en dash; otherwise a comma (or an en dash with mode="en");
   - banned phrases from the template's <style> list: lead-ins are removed, single words use a fix table
     (protocol.yaml `banned_phrase_fixes` extends/overrides DEFAULT_FIXES). Phrases with no safe mechanical fix are reported
     as unresolved, never guessed.
2. Substantive (optional, `use_llm=True`): ONE extra LLM call constrained to the listed protocol violations only. Its output
   is rejected (and logged as rejected) if it introduces any capitalized name or number the original did not contain, so
   revision cannot add authority. Citation findings are never revised.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..budget import Budget
from .audit import LEAK_RE, MCQ_LINE_RE, META_RE, SENT_SPLIT, AuditResult, audit_text
from .design import Cell, Design
from .protocol import Protocol, sha256_text
from .providers import GenRequest, Provider
from .store import StudyStore

DEFAULT_FIXES: dict[str, str] = {
    "it is worth noting": "", "notably": "", "importantly": "", "furthermore": "", "moreover": "", "in conclusion": "",
    "underscores": "shows", "underpins": "supports", "pivotal": "key", "multifaceted": "complex", "comprehensive": "full",
    "robust": "strong",
}
STRICT_PREAMBLE_RE = re.compile(
    r"^\W*(?:here(?:'s| is| are)\b|final answer\b|sure[,!.]|certainly[,!.]|okay[,.!]|let me\b|let's\b|i now have\b|below is\b|"
    r"the following is\b|as an ai\b)", re.I)
MAX_STRIPPED_SENTENCES = 4


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------------------------
# mechanical fixes
# ---------------------------------------------------------------------------------------------------------------
def strip_leading_preamble(text: str) -> tuple[str, list[str]]:
    removed: list[str] = []
    while len(removed) < MAX_STRIPPED_SENTENCES:
        s = text.lstrip()
        if not s:
            break
        first, *rest = SENT_SPLIT.split(s, maxsplit=1)
        if STRICT_PREAMBLE_RE.search(first) or LEAK_RE.search(first) or META_RE.search(first):
            removed.append(first)
            text = rest[0] if rest else ""
        else:
            break
    return (text.lstrip() if removed else text), removed


def extract_mcq_block(text: str, n: int) -> str | None:
    """The last run of lines numbered n, n-1, ... 1 (blank lines ignored), in order; None if there is no such run."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    idx = [i for i, ln in enumerate(lines) if (m := MCQ_LINE_RE.match(ln)) and int(m.group(1)) == n]
    for end in reversed(idx):
        want, i, block = n, end, []
        while i >= 0 and want >= 1:
            m = MCQ_LINE_RE.match(lines[i])
            if m and int(m.group(1)) == want:
                block.append(lines[i].strip())
                want -= 1
            elif not m:
                break
            else:
                break
            i -= 1
        if want == 0:
            return "\n".join(reversed(block)) + "\n"
    return None


def fix_em_dashes(text: str, mode: str = "comma") -> tuple[str, int]:
    n = text.count("—")
    if not n:
        return text, 0
    text = re.sub(r"(?<=\d)\s*—\s*(?=\d)", "–", text)

    def repl(m: re.Match) -> str:
        s, e = m.start(), m.end()
        at_start = s == 0 or text[:s].endswith("\n")
        at_end = e >= len(text) or text[e:e + 1] == "\n" or not text[e:].strip(" ")
        if at_start or at_end:
            return ""
        if mode == "en":
            return " – "
        return ", "

    return re.sub(r"[ \t]*—[ \t]*", repl, text), n


def _sentence_start(before: str) -> bool:
    b = before.rstrip(" \t")
    return not b or b.endswith("\n") or bool(re.search(r"[.!?][\"')\]]*$", b))


def fix_banned_phrases(text: str, banned: list[str], fixes: dict[str, str]) -> tuple[str, list[dict], list[dict]]:
    steps, unresolved = [], []
    table = {**DEFAULT_FIXES, **fixes}
    for ph in sorted(banned, key=len, reverse=True):
        key = ph.lower()
        if key not in table:
            continue
        rep = table[key]
        if rep == "":
            pat = re.compile(rf"\b{re.escape(ph)}\b(?:\s+that\b)?[,]?[ \t]*", re.I)
        else:
            pat = re.compile(rf"\b{re.escape(ph)}\b", re.I)
        count = 0
        for m in reversed(list(pat.finditer(text))):
            before, after = text[:m.start()], text[m.end():]
            if rep == "":
                if _sentence_start(before) and after:
                    after = after[0].upper() + after[1:]
                text = before + after
            else:
                new = rep.capitalize() if m.group(0)[:1].isupper() else rep
                text = before + new + after
            count += 1
        if count:
            steps.append({"kind": "banned_phrase", "phrase": ph, "action": "removed" if rep == "" else f"replaced with {rep!r}",
                          "count": count})
    low = text.lower()
    for ph in banned:
        for m in re.finditer(re.escape(ph.lower()), low):
            unresolved.append({"kind": "banned_phrase", "phrase": ph, "line": text.count("\n", 0, m.start()) + 1,
                               "note": "no safe mechanical fix (needs a sentence-level rewrite): human or LLM pass"})
    return text, steps, unresolved


def mechanical_revise(text: str, comp, protocol: Protocol, audit: AuditResult, *, dash_mode: str = "comma"
                      ) -> tuple[str, list[dict], list[dict]]:
    steps: list[dict] = []
    if audit.checks.get("preamble") == "fail" or audit.checks.get("materials_leak") == "fail":
        if comp.kind == "essay":
            text, removed = strip_leading_preamble(text)
            if removed:
                steps.append({"kind": "preamble_strip", "removed_sentences": removed})
    if comp.kind == "mcq" and comp.answer_format == "letters" and audit.checks.get("format") == "fail":
        block = extract_mcq_block(text, comp.n_questions or 0)
        if block is not None and block != text:
            steps.append({"kind": "mcq_answer_block", "note": "kept only the final 1..N answer block; commentary removed",
                          "removed_chars": len(text) - len(block)})
            text = block
    text, n = fix_em_dashes(text, dash_mode)
    if n:
        steps.append({"kind": "em_dash", "count": n, "mode": f"{dash_mode} (en dash between digits)"})
    text, bsteps, unresolved = fix_banned_phrases(text, protocol.banned_phrases(comp.kind), protocol.banned_phrase_fixes)
    steps += bsteps
    return text, steps, unresolved


# ---------------------------------------------------------------------------------------------------------------
# substantive pass (optional LLM)
# ---------------------------------------------------------------------------------------------------------------
_CAP = re.compile(r"(?<![.!?\n]\s)(?<!^)\b[A-Z][A-Za-z'’-]+")


def new_authority_tokens(original: str, revised: str) -> list[str]:
    """Capitalized mid-sentence words and 3+ digit numbers in `revised` that never occur in `original`."""
    orig_words = {w.lower() for w in re.findall(r"[A-Za-z'’-]+", original)}
    orig_nums = set(re.findall(r"\d[\d,.]*", original))
    new = []
    for m in _CAP.finditer(revised):
        w = m.group(0)
        if w.lower() not in orig_words:
            new.append(w)
    for n in re.findall(r"\d[\d,.]*", revised):
        if len(re.sub(r"\D", "", n)) >= 3 and n not in orig_nums:
            new.append(n)
    return sorted(set(new))


def llm_prompt(text: str, violations: list[dict], comp) -> str:
    bullets = "\n".join(f"- {v['message']}" + (f" (line {v['line']}: {v['evidence']!r})" if v.get("line") and v.get("evidence") else "")
                        for v in violations)
    extra = f" The hard ceiling is {comp.word_ceiling} words; cut from the weakest issue, never the strongest." if comp.word_ceiling else ""
    return ("You are making a minimal protocol-compliance edit to an exam answer. Fix ONLY the violations listed below. Do not add, "
            "remove or change any legal analysis, rule statement or conclusion beyond what a listed fix strictly requires. Do not "
            "add any case name, statute, citation, name or number that is not already in the text. Do not use em dashes."
            f"{extra} Output only the complete revised answer: no preamble, no notes, no headers.\n\n"
            f"VIOLATIONS TO FIX:\n{bullets}\n\nANSWER:\n<<<\n{text}\n>>>")


@dataclass
class RevisionOutcome:
    cell_id: str
    revised: bool
    reason: str = ""
    steps: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    human_review: list[str] = field(default_factory=list)
    llm: dict = field(default_factory=dict)
    post_audit: dict = field(default_factory=dict)


async def revise_cell(design: Design, store: StudyStore, cell: Cell, protocol: Protocol, *, use_llm: bool = False,
                      provider: Provider | None = None, budget: Budget | None = None, reason: str = "", dash_mode: str = "comma",
                      citations=True) -> RevisionOutcome:
    comp = design.component(cell.component)
    meta = store.read_meta(cell)
    original = store.read_capture(cell)  # read-only; this module never writes to capture_path
    banned = protocol.banned_phrases(comp.kind)
    audit = audit_text(original, comp, banned=banned, cell_id=cell.id, sha256=meta["capture_sha256"],
                       truncated=bool(meta.get("truncated")), citations=citations)
    if audit.passed:
        return RevisionOutcome(cell.id, False, reason="capture passes the audit; no revision needed")

    text, steps, unresolved = mechanical_revise(original, comp, protocol, audit, dash_mode=dash_mode)
    llm_log: dict = {"used": False}
    if use_llm:
        mid = audit_text(text, comp, banned=banned, cell_id=cell.id, citations=False)
        fixable = [f.to_dict() for f in mid.violations if f.check not in ("citations", "completeness")]
        if fixable:
            if provider is None:
                raise ValueError("use_llm=True needs a provider")
            model = design.model(cell.model)
            req = GenRequest(cell_id=f"{cell.id}__revise", model=model.model, system="", user=llm_prompt(text, fixable, comp),
                             max_tokens=design.max_tokens_for(comp, model), effort=design.effort_for(comp, model),
                             purpose=f"study-revise:{cell.id}", hint={"kind": comp.kind, "word_ceiling": comp.word_ceiling,
                                                                       "n_questions": comp.n_questions, "revise": True})
            res = await provider.generate(req, budget=budget)
            llm_log = {"used": True, "model": model.model, "served_model": res.served_model, "usage": res.usage.to_dict(),
                       "prompt_sha256": sha256_text(req.user), "violations_sent": [f["message"] for f in fixable],
                       "response_sha256": sha256_text(res.text or ""), "accepted": False}
            if not res.ok:
                llm_log["rejection_reason"] = res.error or "no text returned"
            elif res.truncated:
                llm_log["rejection_reason"] = "response truncated at max_tokens"
            else:
                new = new_authority_tokens(original, res.text or "")
                if new:
                    llm_log["rejection_reason"] = f"introduced names/numbers absent from the original: {new[:10]}"
                else:
                    text, llm_log["accepted"] = res.text or "", True
                    steps.append({"kind": "llm_revision", "note": "one constrained LLM call; see llm log",
                                  "violations": [f["message"] for f in fixable]})
                    # mechanical fixes again: the LLM may have re-introduced a dash
                    text, n = fix_em_dashes(text, dash_mode)
                    if n:
                        steps.append({"kind": "em_dash", "count": n, "mode": dash_mode})

    post = audit_text(text, comp, banned=banned, cell_id=cell.id, target="revised", sha256=sha256_text(text),
                      citations=citations)
    human = [f"{f.check}: {f.message}" for f in post.violations]
    changed = text != original
    outcome = RevisionOutcome(cell.id, changed, steps=steps, unresolved=unresolved, human_review=human, llm=llm_log,
                              post_audit=post.to_dict())
    if not changed:
        outcome.reason = "no deterministic fix applies; violations remain for human review" + ("" if not use_llm else " (LLM pass rejected or unused)")
        return outcome
    counts: dict[str, int] = {}
    for f in audit.violations:
        counts[f.check] = counts.get(f.check, 0) + 1
    outcome.reason = (reason + " " if reason else "") + "Protocol violations in the verbatim capture: " + \
        ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())) + "."
    store.revised_path(cell).write_text(text, encoding="utf-8")
    diff = "".join(difflib.unified_diff(original.splitlines(keepends=True), text.splitlines(keepends=True),
                                        fromfile=f"captures/{cell.id}.md", tofile=f"revisions/{cell.id}.revised.md"))
    store.revision_diff_path(cell).write_text(diff, encoding="utf-8")
    store.revision_meta_path(cell).write_text(json.dumps({
        "cell_id": cell.id, "created_at": _utc(), "reason": outcome.reason, "source_capture_sha256": meta["capture_sha256"],
        "revised_sha256": sha256_text(text), "steps": steps, "unresolved": unresolved, "needs_human_review": human,
        "llm": llm_log, "post_revision_audit": post.to_dict(), "pre_revision_violations": counts,
        "note": "the verbatim capture is unchanged; this is a separate, recorded second pass"}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    store.audit_path(cell, revised=True).write_text(json.dumps(post.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return outcome
