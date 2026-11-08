"""Citation extraction: eyecite first, regex fallback. Spans always index into the ORIGINAL text."""
from __future__ import annotations

import re
import sys
import types
from dataclasses import dataclass, field

CASE, STATUTE, SHORT_CASE, SUPRA, ID, OTHER = "case", "statute", "short_case", "supra", "id", "other"


@dataclass
class Citation:
    kind: str
    raw: str
    span: tuple[int, int]
    full_span: tuple[int, int]
    volume: str | None = None
    reporter: str | None = None
    page: str | None = None
    pin: str | None = None
    year: int | None = None
    court: str | None = None
    plaintiff: str | None = None
    defendant: str | None = None
    written_name: str | None = None  # the case name the author wrote near the cite
    parallels: list[str] = field(default_factory=list)  # normalized parallel cites
    resolved_to: int | None = None  # index (in the extraction list) of the full cite a short form refers to
    extractor: str = "eyecite"

    @property
    def key(self) -> str | None:
        """Normalized cite 'vol reporter page' (case cites) or 'title reporter § section' (statutes)."""
        if self.kind == CASE and self.volume and self.reporter and self.page:
            return f"{self.volume} {self.reporter} {self.page}"
        return None

    @property
    def all_keys(self) -> list[str]:
        return ([self.key] if self.key else []) + list(self.parallels)


def _load_eyecite():
    try:
        import eyecite  # noqa: F401
    except ModuleNotFoundError as e:
        # eyecite imports fast_diff_match_patch only for annotate_citations, which we never use; that C++ wheel does not
        # build on some Pythons (e.g. 3.13). Stub it so extraction still works. Real installs are unaffected.
        if e.name != "fast_diff_match_patch":
            return None
        sys.modules.setdefault("fast_diff_match_patch", types.ModuleType("fast_diff_match_patch"))
        try:
            import eyecite  # noqa: F401
        except Exception:
            return None
    except Exception:
        return None
    return eyecite


_NAME_BACK = re.compile(
    r"((?:In re |Ex parte )[A-Z][\w.'&\- ]{1,60}?|[A-Z][\w.'&\-]*(?:\s+[\w.'&\-]+){0,6}?\s+v\.\s+[A-Z][\w.'&\-,]*(?:\s+[\w.'&\-,]+){0,8}?),?\s*$")


def _name_before(text: str, start: int, limit: int = 140) -> str | None:
    """Best-effort 'X v. Y' immediately before a cite (used when the extractor gives no party names)."""
    window = text[max(0, start - limit):start]
    window = re.split(r"[.;:!?](?=\s+[A-Z])(?<!v\.)", window)[-1] if " v. " in window else window
    m = _NAME_BACK.search(window.rstrip())
    return m.group(1).strip(" ,") if m else None


def _year(v) -> int | None:
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


_POST = re.compile(r"(?:\s*,\s*(?P<pin>\d{1,5}(?:\s*[-\u2013]\s*\d{1,5})?)(?!\s*[A-Za-z]))?[^()]{0,3}?\s*\((?P<paren>[^()]{0,80})\)")
_YEAR = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


def _post_cite(text: str, end: int) -> tuple[str | None, int | None]:
    """(pin, year) read from the text right after a cite: ', 495 (1954)' / ' (9th Cir. 2019)'. eyecite's own year/pin
    are unreliable when several cites share a sentence (it can attach a LATER cite's year), so we parse them ourselves."""
    m = _POST.match(text, end)
    if not m:
        return None, None
    y = _YEAR.search(m.group("paren"))
    return m.group("pin"), int(y.group(1)) if y else None


def extract_eyecite(text: str) -> list[Citation] | None:
    ey = _load_eyecite()
    if ey is None:
        return None
    from eyecite import get_citations, resolve_citations
    from eyecite.models import (FullCaseCitation, FullLawCitation, IdCitation, ShortCaseCitation, SupraCitation)

    raw = get_citations(text)
    out: list[Citation] = []
    for c in raw:
        g, md = c.groups or {}, c.metadata
        s, fs = c.span(), c.full_span()
        if isinstance(c, FullCaseCitation):
            pl, df = getattr(md, "plaintiff", None), getattr(md, "defendant", None)
            name = f"{pl} v. {df}" if pl and df else (pl or df) if (pl or df) else _name_before(text, s[0])
            pin, yr = _post_cite(text, s[1])
            out.append(Citation(CASE, c.matched_text(), s, fs, g.get("volume"), c.corrected_reporter() or g.get("reporter"),
                                g.get("page"), pin or getattr(md, "pin_cite", None), yr,
                                getattr(md, "court", None), pl, df, name))
        elif isinstance(c, ShortCaseCitation):
            out.append(Citation(SHORT_CASE, c.matched_text(), s, fs, g.get("volume"), c.corrected_reporter() or g.get("reporter"),
                                g.get("page"), getattr(md, "pin_cite", None), written_name=getattr(md, "antecedent_guess", None)))
        elif isinstance(c, FullLawCitation):
            sec = g.get("section") or g.get("title")
            out.append(Citation(STATUTE, c.matched_text(), s, fs, g.get("title") or g.get("volume"), c.corrected_reporter() or g.get("reporter"),
                                sec, getattr(md, "pin_cite", None), _year(getattr(md, "year", None))))
        elif isinstance(c, SupraCitation):
            out.append(Citation(SUPRA, c.matched_text(), s, fs, pin=getattr(md, "pin_cite", None),
                                written_name=getattr(md, "antecedent_guess", None)))
        elif isinstance(c, IdCitation):
            out.append(Citation(ID, c.matched_text(), s, fs, pin=getattr(md, "pin_cite", None)))
        else:
            out.append(Citation(OTHER, c.matched_text(), s, fs))
    # resolve id./supra/short forms to the full cite they refer to (by object identity, converted to indexes below)
    target: dict[int, Citation] = {}
    try:
        pos = {id(c): i for i, c in enumerate(raw)}
        for _resource, cites in resolve_citations(raw).items():
            full = [x for x in cites if isinstance(x, FullCaseCitation) and id(x) in pos]
            if full:
                for x in cites:
                    if id(x) in pos and not isinstance(x, FullCaseCitation):
                        target[pos[id(x)]] = out[pos[id(full[0])]]
    except Exception:
        target = {}
    # eyecite emits each reporter of '410 U.S. 113, 93 S. Ct. 705 (1973)' separately, sharing a full_span start:
    # fold later ones into the first as `parallels`
    absorbed: dict[int, Citation] = {}
    kept: list[Citation] = []
    for c in out:
        prev = kept[-1] if kept else None
        if prev is not None and c.kind == CASE and prev.kind == CASE and c.full_span[0] == prev.full_span[0]:
            if c.key:
                prev.parallels.append(c.key)
            prev.year = prev.year or c.year
            absorbed[id(c)] = prev
        else:
            kept.append(c)
    index = {id(c): i for i, c in enumerate(kept)}
    for i, c in enumerate(out):
        t = target.get(i)
        if t is not None:
            c.resolved_to = index.get(id(absorbed.get(id(t), t)))
    return kept


_REPORTERS = (r"U\.S\.|S\. ?Ct\.|L\. ?Ed\. ?2d|L\. ?Ed\.|F\.\s?(?:2d|3d|4th)|F\. ?Supp\. ?(?:2d|3d)?|F\. ?App'x|"
              r"[A-Z][A-Za-z.]{0,6}\.\s?(?:2d|3d|4th)|N\.[EW]\.(?:2d|3d)?|P\.(?:2d|3d)?|A\.(?:2d|3d)?|So\.\s?(?:2d|3d)?|S\.[EW]\.(?:2d|3d)?")
_CASE_RE = re.compile(rf"(?P<vol>\d{{1,4}})\s+(?P<rep>{_REPORTERS})\s+(?P<page>\d{{1,5}})(?:,\s*(?P<pin>\d{{1,5}}))?"
                      rf"(?:\s*\((?P<paren>[^()]{{0,60}}?)\))?")
_STAT_RE = re.compile(r"(?P<title>\d{1,3})\s+(?P<rep>U\.S\.C\.A?|C\.F\.R\.)\s+§§?\s*(?P<sec>[\d.]+[\w()\-]*)")


def extract_regex(text: str) -> list[Citation]:
    """Fallback extractor: common federal/regional reporters and U.S.C./C.F.R. only. Much weaker than eyecite."""
    out: list[Citation] = []
    for m in _CASE_RE.finditer(text):
        yr = re.search(r"(1[6-9]\d\d|20\d\d)", m.group("paren") or "")
        rep = re.sub(r"\s+", " ", m.group("rep"))
        out.append(Citation(CASE, m.group(0), m.span(), m.span(), m.group("vol"), rep, m.group("page"), m.group("pin"),
                            int(yr.group(1)) if yr else None, written_name=_name_before(text, m.start()), extractor="regex"))
    for m in _STAT_RE.finditer(text):
        out.append(Citation(STATUTE, m.group(0), m.span(), m.span(), m.group("title"), m.group("rep"), m.group("sec"),
                            extractor="regex"))
    out.sort(key=lambda c: c.span)
    return out


def extract(text: str, *, prefer: str = "eyecite") -> list[Citation]:
    """All citations in `text`, in document order, with spans into `text`. eyecite first, regex if unavailable."""
    got = extract_eyecite(text) if prefer == "eyecite" else None
    cites = got if got is not None else extract_regex(text)
    for c in cites:
        if c.kind == CASE and not c.written_name:
            c.written_name = _name_before(text, c.span[0])
    return cites
