"""Expected-authorities matching: which instructor-listed authorities the answer cited, missed, or went beyond."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .checks import name_similarity
from .extract import CASE, STATUTE, extract
from .report import CitationReport, ExpectedResult


class Expected:
    def __init__(self, label: str, names: list[str], cites: list[str], statute: str | None = None):
        self.label, self.names, self.cites, self.statute = label, names, cites, statute


def _norm_statute(c) -> str:
    return re.sub(r"\s+", " ", f"{c.volume} {c.reporter} § {c.page}".lower())


def parse_expected(items) -> list[Expected]:
    """Accepts a list (or {authorities: [...]}) of strings or {name, cite, aliases}. A string may be a name
    ("Brown v. Board of Education"), a cite ("347 U.S. 483"), or both ("Brown v. Board, 347 U.S. 483 (1954)")."""
    if isinstance(items, dict):
        items = items.get("authorities", [])
    out: list[Expected] = []
    for it in items or []:
        if isinstance(it, str):
            it = {"name": it}
        label = it.get("name") or it.get("cite") or str(it)
        text = " ; ".join(str(x) for x in [it.get("name"), *(it.get("cite") if isinstance(it.get("cite"), list) else [it.get("cite")])] if x)
        cites, statute = [], None
        for c in extract(text):
            if c.kind == CASE and c.key:
                cites += c.all_keys
            elif c.kind == STATUTE:
                statute = _norm_statute(c)
        name = it.get("name")
        if name:  # drop an inline cite/year from the name
            name = re.split(r",\s*\d+\s+[A-Z]", name)[0].strip(" ,")
            name = re.sub(r"\s*\(\d{4}\)\s*$", "", name)
        names = [n for n in [name, *(it.get("aliases") or [])] if n]
        out.append(Expected(label, names, cites, statute))
    return out


def load_expected(path: str | Path) -> list[Expected]:
    return parse_expected(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def _party_words(name: str) -> list[str]:
    first = re.split(r"\s+v\.?\s+", name, maxsplit=1)[0]
    return [first] if len(first) >= 3 else []


def match_expected(report: CitationReport, expected: list[Expected], text: str | None = None,
                   name_threshold: float = 0.75) -> ExpectedResult:
    """cited: via 'cite' (reporter match, incl. parallels and CourtListener's matched cites), 'name' (fuzzy against the
    name written beside a cite or the verified case name) or 'mention' (name/first party appears in the text with no cite).
    extra: distinct case/statute authorities cited but not matched to any expected entry."""
    res = ExpectedResult()
    used: set[str] = set()
    for e in expected:
        hit = None
        for i, c in enumerate(report.citations):
            keys = set(([c.cite] if c.cite else []) + c.parallels) | {k for m in c.matches for k in getattr(m, "cites", [])}
            if c.kind == STATUTE and e.statute and e.statute == re.sub(r"\s+", " ", c.cite or "").lower():
                hit = (i, "cite")
            elif keys & set(e.cites):
                hit = (i, "cite")
            else:
                names = [n for n in [c.written_name, *[m.case_name for m in c.matches]] if n]
                if c.kind in (CASE,) and any(name_similarity(n, en) >= name_threshold for n in names for en in e.names):
                    hit = (i, "name")
            if hit:
                break
        if hit is None and text and e.names:
            for en in e.names:
                if re.search(r"\b" + re.escape(en) + r"\b", text, re.I) or any(
                        re.search(r"\b" + re.escape(p) + r"\b", text) for p in _party_words(en)):
                    hit = (None, "mention")
                    break
        if hit:
            if hit[0] is not None:
                used.add(report.citations[hit[0]].cite or report.citations[hit[0]].text)
            res.cited.append({"expected": e.label, "via": hit[1],
                              "matched": report.citations[hit[0]].text if hit[0] is not None else None})
        else:
            res.missed.append(e.label)
    seen: set[str] = set()
    for i, c in enumerate(report.citations):
        if c.kind not in (CASE, STATUTE) or (c.cite or c.text) in used:
            continue
        k = c.cite or c.text
        if k in seen:
            continue
        seen.add(k)
        label = (c.matches[0].case_name if c.matches else None) or c.written_name or c.text
        res.extra.append({"label": f"{label} ({c.cite or c.text})" if label != c.text else c.text, "cite": c.cite, "status": c.status})
    return res
