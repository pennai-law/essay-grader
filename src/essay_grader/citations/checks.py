"""Per-citation consistency checks: case name (fuzzy), year, pin-cite plausibility."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_ABBR = {"bd": "board", "educ": "education", "co": "company", "corp": "corporation", "inc": "", "ltd": "", "llc": "",
         "dept": "department", "dep't": "department", "ass'n": "association", "assn": "association", "assoc": "association",
         "comm'n": "commission", "commn": "commission", "comm": "commission", "cnty": "county", "univ": "university",
         "natl": "national", "nat'l": "national", "int'l": "international", "intl": "international", "gov't": "government",
         "govt": "government", "ins": "insurance", "sch": "school", "dist": "district", "mfg": "manufacturing",
         "bros": "brothers", "fin": "financial", "bk": "bank", "sec": "securities", "comm'r": "commissioner",
         "v": "", "vs": "", "of": "", "the": "", "and": "", "et": "", "al": "", "a": "", "in": "", "re": "", "ex": "", "rel": "",
         "us": "united states", "united": "united", "states": "states"}


def _tokens(name: str) -> list[str]:
    s = name.lower().replace("’", "'")
    s = re.sub(r"\bu\.s\.", "united states", s)
    words = re.findall(r"[a-z0-9][a-z0-9']*", s)
    out: list[str] = []
    for w in words:
        out += _ABBR.get(w, w).split()
    return out


def _party_score(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    overlap = len(sa & sb) / min(len(sa), len(sb))
    ratio = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    return max(overlap, ratio)


def name_similarity(written: str, official: str) -> float:
    """0..1. Compares the whole names and, separately, each party the author wrote against the official name, so a
    short-form 'Brown v. Board' still matches 'Brown v. Board of Education of Topeka'."""
    whole = SequenceMatcher(None, " ".join(_tokens(written)), " ".join(_tokens(official))).ratio()
    parts = [p for p in re.split(r"\s+v\.?s?\.?\s+", written, maxsplit=1, flags=re.I) if p.strip()]
    off = _tokens(official)
    per_party = [_party_score(_tokens(p), off) for p in parts]
    party = (sum(per_party) / len(per_party)) if per_party else 0.0
    return max(whole, party)


def name_consistent(written: str | None, official_names: list[str], threshold: float = 0.6) -> tuple[bool | None, float | None]:
    """(ok, best_score); ok is None when there is nothing to compare."""
    if not written or not official_names:
        return None, None
    best = max(name_similarity(written, o) for o in official_names)
    return best >= threshold, round(best, 3)


def year_consistent(written: int | None, official_years: list[int], tolerance: int = 0) -> bool | None:
    if written is None or not official_years:
        return None
    return any(abs(written - y) <= tolerance for y in official_years)


def pin_plausible(first_page: str | None, pin: str | None, max_span: int = 250) -> bool | None:
    """A pin page must be >= the first page and within `max_span` pages of it (very few opinions run longer)."""
    try:
        f = int(first_page)
        m = re.match(r"\d+", pin or "")
        if not m:
            return None
        return 0 <= int(m.group(0)) - f <= max_span
    except (TypeError, ValueError):
        return None
