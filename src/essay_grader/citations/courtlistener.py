"""CourtListener citation-lookup adapter (Free Law Project): batching, backoff, sqlite cache.

Privacy: only the normalized cites ("347 U.S. 483; 410 U.S. 113") are sent, never the student's text.
All knowledge of the remote API's wire format lives in `CourtListenerClient._lookup_batch` and `_parse_item`.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

LOOKUP_URL = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
TOKEN_ENV = "COURTLISTENER_API_TOKEN"

FOUND, NOT_FOUND, AMBIGUOUS, INVALID_REPORTER, ERROR, NOT_VERIFIED = (
    "found", "not_found", "ambiguous", "invalid_reporter", "error", "not_verified")


@dataclass
class Cluster:
    case_name: str = ""
    date_filed: str | None = None
    court: str | None = None
    url: str | None = None
    cluster_id: int | None = None
    citations: list[str] = field(default_factory=list)

    @property
    def year(self) -> int | None:
        m = re.match(r"(\d{4})", self.date_filed or "")
        return int(m.group(1)) if m else None


@dataclass
class Lookup:
    """Result of verifying one normalized cite."""
    cite: str
    status: str
    clusters: list[Cluster] = field(default_factory=list)
    message: str = ""
    from_cache: bool = False

    def to_json(self) -> str:
        return json.dumps({"cite": self.cite, "status": self.status, "message": self.message,
                           "clusters": [c.__dict__ for c in self.clusters]})

    @classmethod
    def from_json(cls, s: str) -> "Lookup":
        d = json.loads(s)
        return cls(d["cite"], d["status"], [Cluster(**c) for c in d["clusters"]], d.get("message", ""), True)


class CourtListenerError(RuntimeError):
    pass


class LookupCache:
    """sqlite cache keyed by normalized cite. Found/ambiguous entries live long; not_found entries are short-lived
    because CourtListener keeps adding opinions. Errors are never cached. Use path=':memory:' for no persistence."""

    def __init__(self, path: str | Path | None = None, *, ttl_found: float = 90 * 86400, ttl_missing: float = 7 * 86400,
                 clock: Callable[[], float] = time.time):
        if path is None:
            path = os.environ.get("ESSAY_GRADER_CACHE") or Path.home() / ".cache" / "essay_grader" / "citations.sqlite"
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()  # the pipeline calls check() from worker threads
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS lookups (cite TEXT PRIMARY KEY, stored REAL NOT NULL, body TEXT NOT NULL)")
        self.ttl_found, self.ttl_missing, self.clock = ttl_found, ttl_missing, clock

    def get(self, cite: str) -> Lookup | None:
        with self.lock:
            row = self.db.execute("SELECT stored, body FROM lookups WHERE cite=?", (cite,)).fetchone()
        if not row:
            return None
        lk = Lookup.from_json(row[1])
        ttl = self.ttl_found if lk.status in (FOUND, AMBIGUOUS) else self.ttl_missing
        return lk if self.clock() - row[0] <= ttl else None

    def put(self, lk: Lookup) -> None:
        if lk.status in (ERROR, NOT_VERIFIED):
            return
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO lookups VALUES (?,?,?)", (lk.cite, self.clock(), lk.to_json()))
            self.db.commit()

    def close(self) -> None:
        self.db.close()


class CourtListenerClient:
    def __init__(self, token: str | None = None, *, cache: LookupCache | None = None, transport: httpx.BaseTransport | None = None,
                 url: str = LOOKUP_URL, max_retries: int = 5, base_delay: float = 2.0, max_delay: float = 60.0,
                 batch_size: int = 100, max_batch_chars: int = 50_000, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 30.0):
        self.token = token if token is not None else os.environ.get(TOKEN_ENV)
        self.cache = cache if cache is not None else LookupCache()
        self.url, self.max_retries, self.base_delay, self.max_delay = url, max_retries, base_delay, max_delay
        self.batch_size, self.max_batch_chars, self.sleep = batch_size, max_batch_chars, sleep
        self.http = httpx.Client(transport=transport, timeout=timeout)
        self.requests_made = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def verify(self, cites: list[str]) -> dict[str, Lookup]:
        """Verify normalized cites ('347 U.S. 483'). Cached results are reused; the rest go out in batched requests."""
        out: dict[str, Lookup] = {}
        todo: list[str] = []
        for c in dict.fromkeys(cites):
            hit = self.cache.get(c)
            if hit is not None:
                out[c] = hit
            else:
                todo.append(c)
        if not todo:
            return out
        if not self.enabled:
            msg = f"{TOKEN_ENV} is not set; nothing was sent to CourtListener"
            return out | {c: Lookup(c, NOT_VERIFIED, message=msg) for c in todo}
        for batch in self._batches(todo):
            try:
                results = self._lookup_batch(batch)
            except CourtListenerError as e:
                results = {c: Lookup(c, ERROR, message=str(e)) for c in batch}
            # items the API throttled per-cite (status 429) get one more round after a pause
            throttled = [c for c, r in results.items() if r.status == ERROR and "429" in r.message]
            if throttled:
                self.sleep(self.base_delay)
                try:
                    results.update(self._lookup_batch(throttled))
                except CourtListenerError as e:
                    results.update({c: Lookup(c, ERROR, message=str(e)) for c in throttled})
            for c, r in results.items():
                self.cache.put(r)
                out[c] = r
        return out

    def _batches(self, cites: list[str]):
        cur: list[str] = []
        size = 0
        for c in cites:
            if cur and (len(cur) >= self.batch_size or size + len(c) + 2 > self.max_batch_chars):
                yield cur
                cur, size = [], 0
            cur.append(c)
            size += len(c) + 2
        if cur:
            yield cur

    # VERIFY against CourtListener citation-lookup docs
    def _lookup_batch(self, cites: list[str]) -> dict[str, Lookup]:
        """POST the cites as one `text` form field; retry 429/5xx/transport errors with exponential backoff.
        Assumed response (verify against the docs): a JSON list of
        {"citation": str, "normalized_citations": [str], "start_index": int, "end_index": int,
         "status": 200|300|400|404|429, "error_message": str,
         "clusters": [{"id", "case_name", "date_filed", "absolute_url", "citations": [{"volume","reporter","page"}], ...}]}
        where 200=found, 300=ambiguous, 400=invalid reporter, 404=not found, 429=throttled for that cite."""
        text = "; ".join(cites)
        spans, pos = [], 0
        for c in cites:
            spans.append((pos, pos + len(c)))
            pos += len(c) + 2
        headers = {"Authorization": f"Token {self.token}"}
        last = ""
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.sleep(delay)
            delay = min(self.max_delay, self.base_delay * 2 ** attempt)
            try:
                self.requests_made += 1
                r = self.http.post(self.url, data={"text": text}, headers=headers)
            except httpx.TransportError as e:
                last = f"transport error: {type(e).__name__}"
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = min(self.max_delay, float(ra))
                continue
            if r.status_code in (401, 403):
                raise CourtListenerError(f"HTTP {r.status_code}: token rejected; check {TOKEN_ENV}")
            if r.status_code != 200:
                raise CourtListenerError(f"HTTP {r.status_code}")
            try:
                return self._parse(r.json(), cites, spans)
            except (ValueError, KeyError, TypeError) as e:
                raise CourtListenerError(f"unexpected response shape: {e}") from e
        raise CourtListenerError(f"gave up after {self.max_retries} retries ({last})")

    @staticmethod
    def _parse(items: list[dict], cites: list[str], spans: list[tuple[int, int]]) -> dict[str, Lookup]:
        res: dict[str, Lookup] = {}
        for it in items:
            idx = None
            for i, (s, e) in enumerate(spans):  # prefer the offsets we know; fall back to string match
                if it.get("start_index") == s and it.get("end_index") == e:
                    idx = i
            if idx is None:
                names = [it.get("citation", "")] + list(it.get("normalized_citations") or [])
                idx = next((i for i, c in enumerate(cites) if c in names), None)
            if idx is None:
                continue
            clusters = [Cluster(cl.get("case_name") or cl.get("caseName") or "", cl.get("date_filed") or cl.get("dateFiled"),
                                cl.get("court") or cl.get("court_id"), _abs(cl.get("absolute_url")), cl.get("id"),
                                [_fmt_cite(x) for x in cl.get("citations") or []])
                        for cl in it.get("clusters") or []]
            code = it.get("status")
            status = {200: FOUND, 300: AMBIGUOUS, 400: INVALID_REPORTER, 404: NOT_FOUND}.get(code)
            if status == FOUND and len(clusters) > 1:
                status = AMBIGUOUS
            if status is None:
                status = ERROR
            msg = it.get("error_message") or ("" if status != ERROR else f"item status {code}")
            if status == ERROR and code == 429:
                msg = "429 throttled"
            res[cites[idx]] = Lookup(cites[idx], status, clusters, msg)
        for c in cites:  # a cite the API said nothing about is not evidence it is fake
            res.setdefault(c, Lookup(c, ERROR, message="no result returned for this cite"))
        return res


def _abs(u: str | None) -> str | None:
    return None if not u else (u if u.startswith("http") else "https://www.courtlistener.com" + u)


def _fmt_cite(x) -> str:
    return x if isinstance(x, str) else f"{x.get('volume')} {x.get('reporter')} {x.get('page')}"
