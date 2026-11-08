"""Offline tests for citation extraction/verification. Fictional memo; no real network (httpx MockTransport)."""
import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from essay_grader import citations as C
from essay_grader.citations.courtlistener import CourtListenerClient, LookupCache

MEMO = (
    "MEMORANDUM (fictional). Segregated public schools are unconstitutional. Brown v. Bd. of Educ., 347 U.S. 483, 495 (1954); "
    "id. at 490. The Court later reaffirmed the point. Brown, 347 U.S. at 494. Roe v. Wade, 410 U.S. 113, 93 S. Ct. 705 (1973). "
    "Plaintiff also sues under 42 U.S.C. § 1983. Smith v. Jones, 999 F.3d 1234, 1240 (9th Cir. 2019) is controlling."
)

DB = {
    "347 U.S. 483": (200, [{"id": 1, "case_name": "Brown v. Board of Education of Topeka", "date_filed": "1954-05-17",
                            "court": "scotus", "absolute_url": "/opinion/1/brown/"}]),
    "410 U.S. 113": (200, [{"id": 2, "case_name": "Roe v. Wade", "date_filed": "1973-01-22", "absolute_url": "/opinion/2/roe/"}]),
    "93 S. Ct. 705": (200, [{"id": 2, "case_name": "Roe v. Wade", "date_filed": "1973-01-22", "absolute_url": "/opinion/2/roe/"}]),
}


def make_client(db=None, *, token="tok", log=None, handler=None, **kw):
    db = DB if db is None else db

    def h(req: httpx.Request):
        if log is not None:
            log.append(req)
        if handler:
            return handler(req)
        assert req.headers["Authorization"] == "Token tok"
        text = parse_qs(req.content.decode())["text"][0]
        out, pos = [], 0
        for cite in text.split("; "):
            code, cl = db.get(cite, (404, []))
            out.append({"citation": cite, "normalized_citations": [cite], "start_index": pos, "end_index": pos + len(cite),
                        "status": code, "error_message": "" if code == 200 else "x", "clusters": cl})
            pos += len(cite) + 2
        return httpx.Response(200, json=out)

    return CourtListenerClient(token, cache=LookupCache(":memory:"), transport=httpx.MockTransport(h),
                               sleep=kw.pop("sleep", lambda s: None), **kw)


def by_cite(rep, cite):
    return next(c for c in rep.citations if c.cite == cite)


# ---------- extraction ----------

def test_extraction_kinds_spans_and_parallels():
    cs = C.extract(MEMO)
    kinds = [c.kind for c in cs]
    assert kinds == ["case", "id", "short_case", "case", "statute", "case"]
    brown = cs[0]
    assert (brown.volume, brown.reporter, brown.page, brown.pin, brown.year) == ("347", "U.S.", "483", "495", 1954)
    assert MEMO[brown.span[0]:brown.span[1]] == "347 U.S. 483"
    assert "Brown" in brown.written_name and "Educ" in brown.written_name
    assert cs[2].resolved_to == 0 and cs[1].resolved_to == 0  # id. and short form resolve to Brown
    roe = cs[3]
    assert roe.key == "410 U.S. 113" and roe.parallels == ["93 S. Ct. 705"]
    assert cs[5].key == "999 F.3d 1234" and cs[5].year == 2019


def test_regex_fallback_extracts_basics():
    cs = C.extract(MEMO, prefer="regex")
    keys = {c.key for c in cs if c.kind == "case"}
    assert {"347 U.S. 483", "410 U.S. 113", "999 F.3d 1234"} <= keys
    assert any(c.kind == "statute" and c.reporter == "U.S.C." for c in cs)
    assert all(c.extractor == "regex" for c in cs)


# ---------- verification statuses ----------

def test_found_not_found_and_statute():
    rep = C.check(MEMO, client=make_client())
    assert rep.verified
    brown = by_cite(rep, "347 U.S. 483")
    assert brown.status == "found" and not brown.flags
    assert brown.matches[0].url == "https://www.courtlistener.com/opinion/1/brown/"
    roe = by_cite(rep, "410 U.S. 113")
    assert roe.status == "found" and roe.parallels == ["93 S. Ct. 705"]
    fake = by_cite(rep, "999 F.3d 1234")
    assert fake.status == "not_found" and C.FABRICATED_SUSPECT in fake.flags
    stat = next(c for c in rep.citations if c.kind == "statute")
    assert stat.status == "not_verified_statute" and C.UNVERIFIABLE in stat.flags
    assert rep.summary["fabricated_suspect"] == 1 and rep.summary["found"] == 2
    shortf = next(c for c in rep.citations if c.kind == "short_case")
    assert shortf.status == "resolved" and shortf.resolves_to == "347 U.S. 483"


def test_ambiguous_and_invalid_reporter():
    db = {"1 U.S. 1": (300, [{"case_name": "Alpha v. Beta", "date_filed": "1800-01-01"},
                              {"case_name": "Gamma v. Delta", "date_filed": "1801-01-01"}]),
          "5 Fake Rptr. 9": (400, [])}
    rep = C.check("See Alpha v. Beta, 1 U.S. 1 (1800).", client=make_client(db))
    c = rep.citations[0]
    assert c.status == "ambiguous" and len(c.matches) == 2 and C.FABRICATED_SUSPECT not in c.flags
    assert C.NAME_MISMATCH not in c.flags  # matches one of the candidates

    def handler(req):
        return httpx.Response(200, json=[{"citation": "5 Fake Rptr. 9", "start_index": 0, "end_index": 14, "status": 400,
                                          "error_message": "Reporter not found", "clusters": []}])
    cl = make_client(handler=handler)
    cl.cache.put  # noqa: B018
    res = cl.verify(["5 Fake Rptr. 9"])["5 Fake Rptr. 9"]
    assert res.status == "invalid_reporter"


def test_no_token_means_unverifiable_and_no_network():
    log = []
    cl = make_client(token="", log=log)
    cl.token = ""
    rep = C.check(MEMO, client=cl)
    assert not rep.verified and not log
    assert all(C.UNVERIFIABLE in c.flags for c in rep.citations if c.kind in ("case", "statute"))
    assert not rep.fabricated_suspects
    assert "NOT checked" in rep.to_markdown()


def test_only_cites_are_sent_not_student_text():
    log = []
    C.check(MEMO, client=make_client(log=log))
    body = log[0].content.decode()
    assert "MEMORANDUM" not in body and "Segregated" not in body


# ---------- consistency checks ----------

def test_name_and_year_mismatch():
    text = "See Marbury v. Madison, 347 U.S. 483 (1960)."
    rep = C.check(text, client=make_client())
    c = rep.citations[0]
    assert c.status == "found"
    assert C.NAME_MISMATCH in c.flags and C.YEAR_MISMATCH in c.flags
    assert C.FABRICATED_SUSPECT not in c.flags


def test_abbreviated_name_is_consistent():
    from essay_grader.citations import checks
    assert checks.name_consistent("Brown v. Bd. of Educ.", ["Brown v. Board of Education of Topeka"])[0]
    assert checks.name_consistent("Brown v. Board", ["Brown v. Board of Education of Topeka"])[0]
    assert checks.name_consistent("Smith v. Jones", ["Brown v. Board of Education of Topeka"])[0] is False


def test_pin_plausibility():
    rep = C.check("See Brown v. Bd. of Educ., 347 U.S. 483, 9999 (1954).", client=make_client())
    assert C.PIN_IMPLAUSIBLE in rep.citations[0].flags
    rep = C.check("See Brown v. Bd. of Educ., 347 U.S. 483, 400 (1954).", client=make_client())
    assert C.PIN_IMPLAUSIBLE in rep.citations[0].flags  # pin before the first page
    rep = C.check("See Brown v. Bd. of Educ., 347 U.S. 483, 495 (1954).", client=make_client())
    assert C.PIN_IMPLAUSIBLE not in rep.citations[0].flags


# ---------- caching / batching / backoff ----------

def test_cache_avoids_repeat_requests(tmp_path):
    log = []
    cache = LookupCache(tmp_path / "c.sqlite")
    cl = CourtListenerClient("tok", cache=cache, transport=httpx.MockTransport(make_client(log=log).http._transport.handler),
                             sleep=lambda s: None)
    C.check(MEMO, client=cl)
    n = len(log)
    assert n == 1  # all cites batched into one request
    C.check(MEMO, client=cl)
    assert len(log) == n  # second pass served entirely from cache (found AND not_found)
    cache2 = LookupCache(tmp_path / "c.sqlite")  # persisted across processes
    assert cache2.get("347 U.S. 483").status == "found" and cache2.get("347 U.S. 483").from_cache


def test_cache_ttl_for_not_found():
    now = [0.0]
    cache = LookupCache(":memory:", ttl_missing=10, clock=lambda: now[0])
    from essay_grader.citations.courtlistener import Lookup
    cache.put(Lookup("1 U.S. 1", "not_found"))
    cache.put(Lookup("2 U.S. 2", "error"))
    assert cache.get("2 U.S. 2") is None  # errors never cached
    now[0] = 11
    assert cache.get("1 U.S. 1") is None


def test_batches_split_by_size():
    log = []
    cl = make_client(log=log, batch_size=2)
    cl.verify(["1 U.S. 1", "2 U.S. 2", "3 U.S. 3", "4 U.S. 4", "5 U.S. 5"])
    assert len(log) == 3


def test_backoff_on_429_then_success():
    sleeps, calls = [], []

    def handler(req):
        calls.append(1)
        if len(calls) <= 2:
            return httpx.Response(429, headers={"Retry-After": "7"} if len(calls) == 1 else {})
        return httpx.Response(200, json=[{"citation": "347 U.S. 483", "start_index": 0, "end_index": 12, "status": 200,
                                          "clusters": [{"case_name": "Brown v. Board", "date_filed": "1954-05-17"}]}])
    cl = make_client(handler=handler, sleep=sleeps.append, base_delay=2.0)
    res = cl.verify(["347 U.S. 483"])["347 U.S. 483"]
    assert res.status == "found" and len(calls) == 3
    assert sleeps == [7.0, 4.0]  # Retry-After honored, then exponential


def test_backoff_gives_up_and_flags_unverifiable_not_fabricated():
    sleeps = []
    cl = make_client(handler=lambda r: httpx.Response(503), sleep=sleeps.append, max_retries=3)
    rep = C.check("Brown v. Bd. of Educ., 347 U.S. 483 (1954).", client=cl)
    c = rep.citations[0]
    assert c.status == "error" and C.UNVERIFIABLE in c.flags and C.FABRICATED_SUSPECT not in c.flags
    assert len(sleeps) == 3 and sleeps == sorted(sleeps)
    assert cl.cache.get("347 U.S. 483") is None  # errors are not cached


def test_per_cite_429_is_retried_once():
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        ok = state["n"] > 1
        return httpx.Response(200, json=[{"citation": "347 U.S. 483", "start_index": 0, "end_index": 12,
                                          "status": 200 if ok else 429, "clusters": [{"case_name": "Brown"}] if ok else []}])
    cl = make_client(handler=handler)
    assert cl.verify(["347 U.S. 483"])["347 U.S. 483"].status == "found" and state["n"] == 2


def test_bad_token_is_an_error_not_a_fabrication():
    cl = make_client(handler=lambda r: httpx.Response(401))
    rep = C.check("Brown v. Bd. of Educ., 347 U.S. 483 (1954).", client=cl)
    assert C.UNVERIFIABLE in rep.citations[0].flags and "token" in " ".join(rep.citations[0].notes)


# ---------- expected authorities ----------

def test_expected_authorities_matching():
    expected = ["Brown v. Board of Education, 347 U.S. 483", "Marbury v. Madison", {"name": "Roe v. Wade", "cite": "410 U.S. 113"},
                "Miranda v. Arizona", "42 U.S.C. § 1983"]
    text = MEMO + " Miranda is not discussed in depth but Miranda v. Arizona is noted."
    rep = C.check(text, client=make_client(), expected=expected)
    e = rep.expected
    cited = {x["expected"]: x["via"] for x in e.cited}
    assert cited["Brown v. Board of Education, 347 U.S. 483"] == "cite"
    assert cited["Roe v. Wade"] == "cite"
    assert cited["Miranda v. Arizona"] == "mention"
    assert cited["42 U.S.C. § 1983"] == "cite"
    assert e.missed == ["Marbury v. Madison"]
    assert [x["cite"] for x in e.extra] == ["999 F.3d 1234"]  # Smith v. Jones is the extra authority
    assert "Expected authorities" in rep.to_markdown()


def test_expected_yaml_forms(tmp_path):
    p = tmp_path / "expected.yaml"
    p.write_text("authorities:\n  - Brown v. Board of Education\n  - name: Roe v. Wade\n    cite: 410 U.S. 113\n")
    exp = C.load_expected(p)
    assert len(exp) == 2 and exp[1].cites == ["410 U.S. 113"]


def test_markdown_report_renders():
    md = C.check(MEMO, client=make_client()).to_markdown()
    assert "fabricated_suspect" in md and "999 F.3d 1234" in md and "courtlistener.com/opinion/1/brown" in md
    json.dumps(C.check(MEMO, client=make_client()).to_dict())
