"""Text extraction, answer loading (keyed by pseudonymous ID), PII warnings, and length policy."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .rubric import LengthPolicy, Rubric

SUPPORTED = {".txt", ".md", ".pdf", ".docx"}


def extract_text(path: str | Path) -> str:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in {".txt", ".md"}:
        return p.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n".join((page.extract_text() or "") for page in PdfReader(str(p)).pages)
    if suffix == ".docx":
        from docx import Document

        return "\n".join(par.text for par in Document(str(p)).paragraphs)
    raise ValueError(f"unsupported file type: {p.name}")


def word_count(text: str) -> int:
    return len(text.split())


@dataclass
class Answer:
    student_id: str  # pseudonymous ID = filename stem (never a real name)
    question_id: str
    text: str
    source: str
    warnings: list[str] = field(default_factory=list)


_NAME_HEADER = re.compile(
    r"^\s*(name|student|submitted by|by|author)\s*[:\-]\s*\S+|^\s*[A-Z][a-z]+(?:\s[A-Z]\.?)?\s[A-Z][a-z]+\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def pii_warnings(text: str) -> list[str]:
    """Cheap heuristic only: warn if a name-like header appears in the first lines. No scrubbing."""
    head = "\n".join(text.strip().splitlines()[:5])
    m = _NAME_HEADER.search(head)
    return [f"possible name-like header near top of answer: {m.group(0).strip()[:40]!r} (answers should be pseudonymous)"] if m else []


def load_answers(directory: str | Path, rubric: Rubric) -> list[Answer]:
    """Files are `<id>.<ext>` for single-question rubrics, else `<id>__<question_id>.<ext>`."""
    d = Path(directory)
    files = sorted(f for f in d.iterdir() if f.suffix.lower() in SUPPORTED and f.is_file())
    if not files:
        raise FileNotFoundError(f"no .txt/.md/.pdf/.docx answers found in {d}")
    qids = [q.id for q in rubric.questions]
    out: list[Answer] = []
    seen: set[tuple[str, str]] = set()
    for f in files:
        sid, sep, qid = f.stem.partition("__")
        if not sep:
            if len(qids) != 1:
                raise ValueError(f"{f.name}: rubric has {len(qids)} questions; name files <id>__<question_id>.<ext>")
            qid = qids[0]
        if qid not in qids:
            raise ValueError(f"{f.name}: unknown question id {qid!r}")
        if (sid, qid) in seen:
            raise ValueError(f"duplicate answer for {sid}/{qid}")
        seen.add((sid, qid))
        text = extract_text(f)
        out.append(Answer(sid, qid, text, f.name, pii_warnings(text)))
    return out


def apply_length_policy(text: str, policy: LengthPolicy) -> tuple[str, dict]:
    """Deterministic, applied BEFORE any grader sees the text.

    Returns (text_to_grade, info) where info has words, words_graded, truncated, deduction.
    """
    words = word_count(text)
    info = {"words": words, "words_graded": words, "truncated": False, "deduction": 0.0}
    cap = policy.word_cap
    if policy.mode == "no_penalty" or cap is None or words <= cap:
        return text, info
    if policy.mode == "grade_first_n_words":
        # keep original whitespace/paragraphs up to the Nth word
        matches = list(re.finditer(r"\S+", text))
        text = text[: matches[cap - 1].end()]
        info.update(words_graded=cap, truncated=True)
        return text, info
    over = words - cap
    ded = policy.deduct_points_per_100_words * over / 100
    if policy.max_deduction is not None:
        ded = min(ded, policy.max_deduction)
    info["deduction"] = round(ded, 4)
    return text, info
