"""CitationReport: per-text results, flags, summary, markdown rendering."""
from __future__ import annotations

from pydantic import BaseModel, Field

FABRICATED_SUSPECT, NAME_MISMATCH, YEAR_MISMATCH, UNVERIFIABLE = "fabricated_suspect", "name_mismatch", "year_mismatch", "unverifiable"
PIN_IMPLAUSIBLE = "pin_implausible"  # extra flag (not one of the four core flags); informational
STATUTE_STATUS = "not_verified_statute"
RESOLVED, UNRESOLVED = "resolved", "unresolved"


class Match(BaseModel):
    case_name: str = ""
    date_filed: str | None = None
    court: str | None = None
    url: str | None = None
    cites: list[str] = Field(default_factory=list)  # all reporter cites CourtListener lists for the cluster


class CitationResult(BaseModel):
    kind: str
    text: str
    span: tuple[int, int]
    cite: str | None = None  # normalized
    parallels: list[str] = Field(default_factory=list)
    pin: str | None = None
    year: int | None = None
    written_name: str | None = None
    status: str  # found | not_found | ambiguous | invalid_reporter | error | not_verified | not_verified_statute | resolved | unresolved
    matches: list[Match] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    name_score: float | None = None
    resolves_to: str | None = None  # for short forms
    extractor: str = "eyecite"


class ExpectedResult(BaseModel):
    cited: list[dict] = Field(default_factory=list)   # {expected, via, matched}
    missed: list[str] = Field(default_factory=list)
    extra: list[dict] = Field(default_factory=list)   # authorities relied on but not on the instructor list


class CitationReport(BaseModel):
    citations: list[CitationResult] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    verifier: str = "courtlistener"
    verified: bool = False  # False when no token / no network: nothing was checked for existence
    expected: ExpectedResult | None = None

    @property
    def flags(self) -> set[str]:
        return {f for c in self.citations for f in c.flags}

    @property
    def fabricated_suspects(self) -> list[CitationResult]:
        return [c for c in self.citations if FABRICATED_SUSPECT in c.flags]

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")

    def to_markdown(self, title: str = "Citation check") -> str:
        s = self.summary
        L = [f"# {title}", "",
             "Automated existence check against CourtListener (free, incomplete). A flag is a prompt for human review, "
             "never a finding of fabrication; see docs/CITATIONS.md for false-positive risks.", ""]
        if not self.verified:
            L += ["> **Existence was NOT checked** (no CourtListener token or the service was unreachable). "
                  "Every case cite is `unverifiable`.", ""]
        L += [f"- Citations extracted: {s.get('total', 0)} ({s.get('cases', 0)} case, {s.get('statutes', 0)} statute/regulation, "
              f"{s.get('short_forms', 0)} short form/id./supra)",
              f"- Found: {s.get('found', 0)}; not found: {s.get('not_found', 0)}; ambiguous: {s.get('ambiguous', 0)}; "
              f"invalid reporter: {s.get('invalid_reporter', 0)}; unverifiable: {s.get('unverifiable', 0)}",
              f"- Flags: fabricated_suspect {s.get('fabricated_suspect', 0)}, name_mismatch {s.get('name_mismatch', 0)}, "
              f"year_mismatch {s.get('year_mismatch', 0)}, unverifiable {s.get('flag_unverifiable', 0)}", ""]
        if self.citations:
            L += ["| # | Citation | Written name | Status | Matched case | Flags |", "|---|---|---|---|---|---|"]
            for i, c in enumerate(self.citations, 1):
                st = c.status + ("" if c.status != STATUTE_STATUS else " (statute)")
                m = c.matches[0] if c.matches else None
                matched = (f"[{m.case_name}]({m.url}) ({(m.date_filed or '')[:4]}, {m.court or '?'})" if m and m.url
                           else (m.case_name if m else (f"-> {c.resolves_to}" if c.resolves_to else "")))
                if len(c.matches) > 1:
                    matched += f" (+{len(c.matches) - 1} more)"
                L.append(f"| {i} | `{_esc(c.text)}` | {_esc(c.written_name or '')} | {st} | {_esc(matched)} | {', '.join(c.flags)} |")
            L.append("")
            for i, c in enumerate(self.citations, 1):
                for n in c.notes:
                    L.append(f"- #{i}: {n}")
            L.append("")
        e = self.expected
        if e:
            L += ["## Expected authorities", "",
                  f"- Cited ({len(e.cited)}): " + ("; ".join(f"{x['expected']} ({x['via']})" for x in e.cited) or "none"),
                  f"- Missed ({len(e.missed)}): " + ("; ".join(e.missed) or "none"),
                  f"- Extra, not on the instructor list ({len(e.extra)}): " + ("; ".join(x['label'] for x in e.extra) or "none"), ""]
        return "\n".join(L)


def _esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")
