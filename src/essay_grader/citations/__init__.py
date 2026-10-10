"""Legal citation extraction and verification (eyecite + CourtListener citation-lookup).

Public API (stable):
    check(text, *, client=None, expected=None) -> CitationReport
    CitationReport.to_markdown() / .to_dict()
    load_expected(path), parse_expected(items), match_expected(report, expected, text)

Verification is an aid for human reviewers. It never decides a grade. See docs/CITATIONS.md.
"""
from __future__ import annotations

from . import checks
from .courtlistener import (AMBIGUOUS, ERROR, FOUND, INVALID_REPORTER, NOT_FOUND, NOT_VERIFIED, CourtListenerClient,
                            CourtListenerError, Lookup, LookupCache)
from .expected import Expected, load_expected, match_expected, parse_expected
from .extract import CASE, ID, SHORT_CASE, STATUTE, SUPRA, Citation, extract
from .report import (FABRICATED_SUSPECT, NAME_MISMATCH, PIN_IMPLAUSIBLE, RESOLVED, STATUTE_STATUS, UNRESOLVED,
                     UNVERIFIABLE, YEAR_MISMATCH, CitationReport, CitationResult, ExpectedResult, Match)

__all__ = ["check", "CitationReport", "CitationResult", "CourtListenerClient", "LookupCache", "Citation", "extract",
           "load_expected", "parse_expected", "match_expected", "Expected", "FABRICATED_SUSPECT", "NAME_MISMATCH",
           "YEAR_MISMATCH", "UNVERIFIABLE", "PIN_IMPLAUSIBLE"]

MAX_PIN_SPAN = 250
NAME_THRESHOLD = 0.6


def _statute_cite(c: Citation) -> str:
    return f"{c.volume} {c.reporter} § {c.page}" if c.volume else f"{c.reporter} § {c.page}"


def check(text: str, *, client: CourtListenerClient | None = None, expected=None) -> CitationReport:
    """Extract every citation in `text`, verify case cites against CourtListener, and flag problems.

    `client` defaults to a CourtListenerClient reading COURTLISTENER_API_TOKEN (no token = nothing sent, case cites are
    flagged `unverifiable`). `expected` (list of strings/dicts, a path-loaded list, or Expected objects) adds an
    expected-authorities comparison. Only normalized cites leave the machine, never the text."""
    cites = extract(text)
    client = client or CourtListenerClient()
    keys = [k for c in cites if c.kind == CASE for k in c.all_keys]
    lookups = client.verify(keys) if keys else {}
    verified = any(lk.status in (FOUND, NOT_FOUND, AMBIGUOUS, INVALID_REPORTER) for lk in lookups.values())

    results: list[CitationResult] = []
    for i, c in enumerate(cites):
        r = CitationResult(kind=c.kind, text=c.raw, span=c.span, cite=c.key, parallels=list(c.parallels), pin=c.pin,
                           year=c.year, written_name=c.written_name, status=NOT_VERIFIED, extractor=c.extractor)
        if c.kind == CASE:
            _judge_case(c, r, lookups)
        elif c.kind == STATUTE:
            r.cite, r.status = _statute_cite(c), STATUTE_STATUS
            r.flags.append(UNVERIFIABLE)
            r.notes.append("statute/regulation: extracted only, not verified")
        else:  # short form, supra, id.
            tgt = cites[c.resolved_to] if c.resolved_to is not None else None
            r.status = RESOLVED if tgt else UNRESOLVED
            r.resolves_to = tgt.key if tgt else None
            if c.kind == SHORT_CASE and tgt and c.pin is not None and checks.pin_plausible(tgt.page, c.pin, MAX_PIN_SPAN) is False:
                r.flags.append(PIN_IMPLAUSIBLE)
                r.notes.append(f"pin {c.pin} is implausible for a case starting at {tgt.page}")
        results.append(r)

    rep = CitationReport(citations=results, verified=verified)
    rep.summary = _summarize(rep)
    if expected is not None:
        exp = expected if isinstance(expected, list) and all(isinstance(x, Expected) for x in expected) else parse_expected(expected)
        rep.expected = match_expected(rep, exp, text)
    return rep


def _judge_case(c: Citation, r: CitationResult, lookups: dict[str, Lookup]) -> None:
    group = [lookups[k] for k in c.all_keys if k in lookups]
    primary = lookups.get(c.key)
    if primary is None:
        r.flags.append(UNVERIFIABLE)
        return
    # existence: found on any reporter of a parallel group beats a miss on another
    best = next((g for g in group if g.status == FOUND), None) or next((g for g in group if g.status == AMBIGUOUS), None) or primary
    r.status = best.status
    r.matches = [Match(case_name=m.case_name, date_filed=m.date_filed, court=m.court, url=m.url, cites=m.citations)
                 for m in best.clusters]
    if best.status in (NOT_FOUND, INVALID_REPORTER):
        r.flags.append(FABRICATED_SUSPECT)
        r.notes.append("not found in CourtListener" if best.status == NOT_FOUND else
                       f"reporter not recognized ({best.message or 'invalid reporter'})")
        r.notes.append("may be an unreported/very recent/state-reporter case; check Westlaw/Lexis before concluding")
        return
    if best.status in (ERROR, NOT_VERIFIED):
        r.flags.append(UNVERIFIABLE)
        if best.message:
            r.notes.append(best.message)
        return
    if best.status == AMBIGUOUS:
        r.notes.append("citation matches multiple cases; name/year checked against any of them")
    # name and year consistency (against any matched cluster when ambiguous)
    ok, score = checks.name_consistent(c.written_name, [m.case_name for m in best.clusters], NAME_THRESHOLD)
    r.name_score = score
    if ok is False:
        r.flags.append(NAME_MISMATCH)
        r.notes.append(f"written name '{c.written_name}' vs matched '{best.clusters[0].case_name}' (similarity {score})")
    y = checks.year_consistent(c.year, [m.year for m in best.clusters if m.year])
    if y is False:
        r.flags.append(YEAR_MISMATCH)
        r.notes.append(f"written year {c.year} vs matched {', '.join(str(m.year) for m in best.clusters if m.year)}")
    if checks.pin_plausible(c.page, c.pin, MAX_PIN_SPAN) is False:
        r.flags.append(PIN_IMPLAUSIBLE)
        r.notes.append(f"pin {c.pin} is implausible for a case starting at {c.page}")


def _summarize(rep: CitationReport) -> dict:
    cs = rep.citations
    n = lambda pred: sum(1 for c in cs if pred(c))  # noqa: E731
    cases = [c for c in cs if c.kind == CASE]
    return {"total": len(cs), "cases": len(cases), "statutes": n(lambda c: c.kind == STATUTE),
            "short_forms": n(lambda c: c.kind in (SHORT_CASE, SUPRA, ID)),
            "found": n(lambda c: c.status == FOUND), "not_found": n(lambda c: c.status == NOT_FOUND),
            "ambiguous": n(lambda c: c.status == AMBIGUOUS), "invalid_reporter": n(lambda c: c.status == INVALID_REPORTER),
            "unverifiable": n(lambda c: UNVERIFIABLE in c.flags), "fabricated_suspect": n(lambda c: FABRICATED_SUSPECT in c.flags),
            "name_mismatch": n(lambda c: NAME_MISMATCH in c.flags), "year_mismatch": n(lambda c: YEAR_MISMATCH in c.flags),
            "flag_unverifiable": n(lambda c: UNVERIFIABLE in c.flags), "pin_implausible": n(lambda c: PIN_IMPLAUSIBLE in c.flags)}
