"""Automated audit: NEXT_SESSION_GUIDE Step 4 ported to code. One JSON per cell.

Checks (each yields findings with severity `violation` | `warn` | `info`):
  word_count      `wc -w` semantics (str.split) vs the component's ceiling; 85-95% target band is a warning only
  em_dash         U+2014 anywhere (U+2013 en dash is allowed)
  banned_phrases  the list parsed from the template's <style> block, case-insensitive substring like the guide's grep -i
  format          headers / bullets / numbered lists / italics / bold markdown in essay prose (MCQ: letters-only layout and count)
  preamble        first sentence of each cell (incl. the FUSED opening-sentence form) and AI/meta-commentary anywhere
  materials_leak  L-043 denylist, whole text, must be zero hits; also "no materials" tells for the A arm
  citations       `essay_grader.citations.check(text)` if that module exists, else skipped with a note

A citation flag is never "fixed": `revise` ignores it and it stays flagged for a human (no fabricated authority).
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Callable

from .design import Component

EM_DASH = "—"

# NEXT_SESSION_GUIDE.md, Step 4 / Step 8: the L-043 denylist (case-insensitive), plus tells for the no-materials condition.
LEAK_PATTERNS = [
    r"course materials",
    r"materials (?:are|were|have been) (?:attached|provided)",
    r"attached (?:the )?course materials",
    r"provided (?:course )?(?:materials|outline)",
    r"i have reviewed the (?:attached|provided|course|materials)",
    r"based on the (?:attached|provided) (?:materials|outline)",
    r"no (?:course )?materials (?:are|were|have been) (?:attached|provided)",
    r"without (?:any )?(?:course )?materials",
]
LEAK_RE = re.compile("|".join(LEAK_PATTERNS), re.I)

PREAMBLE_RE = re.compile(
    r"^\W*(?:here(?:'s| is| are)\b|final answer\b|answer\s*:|sure[,!.]|certainly\b|of course\b|okay[,.!]|ok[,.!]|"
    r"i(?:'ll|'m going to| will| would| am going to| now have| have| need to| can)\b|let me\b|let's\b|below is\b|"
    r"the following is\b|as an ai\b|as a (?:law student|language model)\b|first, i\b)", re.I)
META_RE = re.compile(r"\bas an ai\b|\blanguage model\b|\bi (?:cannot|can't) (?:browse|access)\b|\blet me (?:write|draft|begin|start)\b|"
                     r"\btarget word count\b|\bi now have\b", re.I)
WORD_COUNT_LINE_RE = re.compile(r"^\s*\**word count\**\s*:", re.I | re.M)
HEADER_RE = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)", re.M)
BOLD_RE = re.compile(r"\*\*[^*\n]+\*\*")
ITALIC_RE = re.compile(r"(?<![\w*])\*[A-Za-z][^*\n]*\*(?![\w*])|(?<![\w_])_[A-Za-z][^_\n]*_(?![\w_])")
MCQ_LINE_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*([A-Ea-e])\s*$")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Finding:
    check: str
    severity: str
    message: str
    line: int | None = None
    evidence: str = ""

    def to_dict(self) -> dict:
        d = {"check": self.check, "severity": self.severity, "message": self.message}
        if self.line is not None:
            d["line"] = self.line
        if self.evidence:
            d["evidence"] = self.evidence
        return d


@dataclass
class AuditResult:
    cell_id: str
    target: str  # capture | revised
    sha256: str
    words: int
    findings: list[Finding] = field(default_factory=list)
    citations: dict = field(default_factory=dict)
    checks: dict[str, str] = field(default_factory=dict)

    @property
    def violations(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "violation"]

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {"cell_id": self.cell_id, "target": self.target, "audited_sha256": self.sha256, "passed": self.passed,
                "words": self.words, "violations": len(self.violations),
                "warnings": sum(f.severity == "warn" for f in self.findings), "checks": self.checks,
                "findings": [f.to_dict() for f in self.findings], "citations": self.citations}


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _snip(text: str, start: int, end: int, pad: int = 30) -> str:
    return text[max(0, start - pad):end + pad].replace("\n", " ")


def citations_hook() -> Callable[[str], object] | None:
    """`essay_grader.citations.check` if the module exists and imports cleanly, else None."""
    try:
        mod = importlib.import_module("essay_grader.citations")
    except Exception:
        return None
    return getattr(mod, "check", None)


def _citation_status(report: object) -> tuple[str, dict]:
    """Normalize whatever `check` returns (CitationReport, dict, list). Flagged = fabricated suspects or any flagged cite."""
    def get(name, default=None):
        if isinstance(report, dict):
            return report.get(name, default)
        return getattr(report, name, default)

    suspects = get("fabricated_suspects") or []
    if callable(suspects):
        suspects = suspects()
    cites = get("citations") if not isinstance(report, list) else report
    cites = cites or []

    def flags_of(c):
        return list((c.get("flags") if isinstance(c, dict) else getattr(c, "flags", None)) or [])

    # "unverifiable" (no token / statute) is information, not a defect; anything else is a prompt for human review
    flagged = [c for c in cites if [f for f in flags_of(c) if f != "unverifiable"]]
    verified = get("verified", None)
    try:
        detail = report.to_dict() if hasattr(report, "to_dict") else (report if isinstance(report, (dict, list)) else repr(report))
    except Exception:
        detail = repr(report)
    status = "flagged" if suspects else ("review" if flagged else "ok")
    return status, {"n_suspects": len(suspects), "n_flagged": len(flagged), "n_citations": len(cites), "verified": verified,
                    "report": detail}


def audit_text(text: str, comp: Component, *, banned: list[str], cell_id: str = "", target: str = "capture", sha256: str = "",
               truncated: bool = False, citations: bool | Callable[[str], object] = True) -> AuditResult:
    words = len(text.split())  # == wc -w
    res = AuditResult(cell_id, target, sha256, words)
    F = res.findings

    def done(name: str, fs: list[Finding]):
        F.extend(fs)
        res.checks[name] = "fail" if any(f.severity == "violation" for f in fs) else ("warn" if any(f.severity == "warn" for f in fs) else "pass")

    # truncation / emptiness
    fs = []
    if truncated:
        fs.append(Finding("completeness", "violation", "output was truncated at max_tokens"))
    if not text.strip():
        fs.append(Finding("completeness", "violation", "output is empty"))
    done("completeness", fs)

    # word count
    fs = []
    if comp.kind == "essay":
        if comp.word_ceiling:
            ratio = words / comp.word_ceiling
            lo, hi = comp.target_band
            if words > comp.word_ceiling:
                fs.append(Finding("word_count", "violation", f"{words} words exceeds the ceiling of {comp.word_ceiling} ({ratio:.1%})"))
            elif not lo <= ratio <= hi:
                fs.append(Finding("word_count", "warn", f"{words} words = {ratio:.1%} of the ceiling; target band is {lo:.0%}-{hi:.0%}"))
            else:
                fs.append(Finding("word_count", "info", f"{words} words = {ratio:.1%} of the ceiling"))
        else:
            fs.append(Finding("word_count", "info", f"{words} words (no numeric ceiling in the design)"))
        done("word_count", fs)
    else:
        res.checks["word_count"] = "skipped"

    # em dash
    fs = [Finding("em_dash", "violation", "em dash (U+2014)", line=_line_of(text, m), evidence=_snip(text, m, m + 1))
          for m in [i for i, ch in enumerate(text) if ch == EM_DASH]]
    done("em_dash", fs)

    # banned phrases
    fs = []
    if not banned:
        fs.append(Finding("banned_phrases", "warn", "no banned-phrase list could be parsed from the template <style> block"))
    low = text.lower()
    for ph in banned:
        for m in re.finditer(re.escape(ph.lower()), low):
            fs.append(Finding("banned_phrases", "violation", f"banned phrase {ph!r}", line=_line_of(text, m.start()),
                              evidence=_snip(text, m.start(), m.end())))
    done("banned_phrases", fs)

    # format
    fs = []
    if comp.kind == "essay":
        for m in HEADER_RE.finditer(text):
            fs.append(Finding("format", "violation", "header / bullet / numbered-list line in prose answer",
                              line=_line_of(text, m.start()), evidence=text[m.start():m.start() + 60].strip()))
        for m in BOLD_RE.finditer(text):
            fs.append(Finding("format", "violation", "markdown bold", line=_line_of(text, m.start()), evidence=m.group(0)[:60]))
        for m in ITALIC_RE.finditer(text):
            fs.append(Finding("format", "violation", "italics/markdown emphasis (case names must not be italicized)",
                              line=_line_of(text, m.start()), evidence=m.group(0)[:60]))
        if WORD_COUNT_LINE_RE.search(text):
            fs.append(Finding("format", "warn", "contains a 'WORD COUNT:' line (fine only if the exam requires one)"))
    elif comp.answer_format == "letters":
        seen: list[int] = []
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            m = MCQ_LINE_RE.match(line)
            if m:
                seen.append(int(m.group(1)))
            else:
                fs.append(Finding("format", "violation", "non-answer line in a letters-only MCQ output (preamble/commentary)",
                                  line=i, evidence=line.strip()[:80]))
        n = comp.n_questions or 0
        if seen != list(range(1, n + 1)):
            fs.append(Finding("format", "violation", f"expected answers 1..{n} once each, in order; found {len(seen)} answer line(s)"))
    done("format", fs)

    # preamble / meta-commentary
    fs = []
    stripped = text.lstrip()
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if comp.kind == "essay" and paras:
        first = SENT_SPLIT.split(paras[0].strip(), maxsplit=1)[0]
        if PREAMBLE_RE.search(first):
            fs.append(Finding("preamble", "violation", "opening sentence reads as a preamble", line=1, evidence=first[:120]))
        if (m := WORD_COUNT_LINE_RE.match(stripped)):
            fs.append(Finding("preamble", "violation", "opens with a word-count line", line=1, evidence=m.group(0)))
    for m in META_RE.finditer(text):
        fs.append(Finding("preamble", "violation", "meta-commentary / AI self-reference", line=_line_of(text, m.start()),
                          evidence=_snip(text, m.start(), m.end())))
    done("preamble", fs)

    # L-043 materials leak
    fs = [Finding("materials_leak", "violation", "L-043: discloses (or denies) the materials condition", line=_line_of(text, m.start()),
                  evidence=_snip(text, m.start(), m.end())) for m in LEAK_RE.finditer(text)]
    done("materials_leak", fs)

    # citations hook
    hook = citations_hook() if citations is True else (citations if callable(citations) else None)
    if citations is False:
        res.citations = {"status": "skipped", "note": "citations check disabled for this audit"}
    elif hook is None:
        res.citations = {"status": "skipped", "note": "essay_grader.citations not available; citation existence was NOT checked"}
    elif comp.kind == "mcq" and comp.answer_format == "letters":
        res.citations = {"status": "skipped", "note": "letters-only MCQ output has no citations"}
    else:
        try:
            status, detail = _citation_status(hook(text))
            res.citations = {"status": status, **detail}
            if status == "flagged":
                F.append(Finding("citations", "violation", f"{detail['n_suspects']} fabricated-suspect citation(s): human review "
                                 f"required (never auto-revised, never papered over)"))
            elif status == "review":
                F.append(Finding("citations", "warn", f"{detail['n_flagged']} citation(s) carry flags: human review"))
            elif detail.get("verified") is False and detail.get("n_citations"):
                F.append(Finding("citations", "warn", "citations were extracted but NOT verified (no CourtListener token / network)"))
        except Exception as e:
            res.citations = {"status": "error", "note": f"{type(e).__name__}: {e}"}
            F.append(Finding("citations", "warn", f"citation check raised {type(e).__name__}: {e}"))
    res.checks["citations"] = {"skipped": "skipped", "ok": "pass", "flagged": "fail", "review": "warn", "error": "warn"}[res.citations["status"]]
    return res


def audit_study(design, store, protocol, *, revised: bool = False, citations: bool | Callable[[str], object] = True) -> list[AuditResult]:
    """Audit every captured cell and write `audits/<cell>.audit.json` (or `.revised.audit.json` for revised text)."""
    import json

    out = []
    for cell in store.captured_cells():
        comp = design.component(cell.component)
        meta = store.read_meta(cell)
        if revised:
            if not store.revised_path(cell).is_file():
                continue
            text, target = store.revised_path(cell).read_text(encoding="utf-8"), "revised"
        else:
            text, target = store.read_capture(cell), "capture"
        from .protocol import sha256_text

        res = audit_text(text, comp, banned=protocol.banned_phrases(comp.kind), cell_id=cell.id, target=target,
                         sha256=sha256_text(text), truncated=bool(meta.get("truncated")) and not revised, citations=citations)
        store.audit_path(cell, revised=revised).write_text(json.dumps(res.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        out.append(res)
    return out
