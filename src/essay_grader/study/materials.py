"""Materials boundary (enforced in code) and the materials-naivety check.

Boundary (exam-taker invariant "Materials boundary"): each arm reads ONLY the files in its own materials folder. `ArmSandbox`
is the only door a cell has to materials; anything that resolves outside the folder (`..`, absolute paths, symlinks, sibling
folders with a shared prefix) raises `MaterialsBoundaryError`. Arm A has no folder, so every read raises.

Naivety (invariant "B-arm materials must be naive to the actual exam"): before any run, each arm's materials are scanned for
(1) n-gram overlap with this term's exam question text, (2) the exact essay word limit, (3) the exact MCQ count,
(4) rubric / answer-key / exam-instructions filenames, (5) byte-copies of the exam or key files, and (6) a port of the
NEXT_SESSION_GUIDE Step 1 grep as a non-blocking warning. Blocking findings stop the run.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..ingest import SUPPORTED, extract_text
from .design import Design


class MaterialsBoundaryError(PermissionError):
    pass


class MaterialsError(Exception):
    pass


class MaterialsNaivetyError(Exception):
    def __init__(self, report: "NaivetyReport"):
        super().__init__("materials-naivety check failed:\n" + report.render())
        self.report = report


@dataclass(frozen=True)
class MaterialFile:
    rel: str
    text: str
    sha256: str
    size: int

    def manifest_entry(self) -> dict:
        return {"path": self.rel, "sha256": self.sha256, "bytes": self.size}


@dataclass
class MaterialsBundle:
    arm: str
    files: list[MaterialFile]

    @property
    def manifest(self) -> list[dict]:
        return [f.manifest_entry() for f in self.files]

    @property
    def manifest_sha256(self) -> str:
        blob = "\n".join(f"{f.rel}\t{f.sha256}" for f in self.files)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def render(self) -> str:
        """The text block handed to the model: only this arm's files, nothing else."""
        if not self.files:
            return ""
        parts = ["<attached_materials>"]
        for f in self.files:
            parts.append(f'<file name="{f.rel}">\n{f.text.strip()}\n</file>')
        parts.append("</attached_materials>")
        return "\n".join(parts)


class ArmSandbox:
    def __init__(self, arm_id: str, root: Path | None):
        self.arm_id = arm_id
        self.root = root.resolve() if root is not None else None

    def resolve(self, path: str | Path) -> Path:
        if self.root is None:
            raise MaterialsBoundaryError(f"arm {self.arm_id} has no materials; reading {path} is not allowed")
        p = Path(path)
        p = p if p.is_absolute() else self.root / p
        rp = p.resolve()
        if not rp.is_relative_to(self.root):
            raise MaterialsBoundaryError(f"arm {self.arm_id} may only read inside {self.root}; refused {path} -> {rp}")
        return rp

    def read_text(self, path: str | Path) -> str:
        return extract_text(self.resolve(path))

    def list_files(self) -> list[Path]:
        if self.root is None:
            return []
        out = []
        for f in sorted(self.root.rglob("*")):
            if f.is_dir() or f.name.startswith(".") or any(part.startswith(".") for part in f.relative_to(self.root).parts):
                continue
            self.resolve(f)  # a symlink pointing out of the folder raises here
            out.append(f)
        return out

    def load(self) -> MaterialsBundle:
        files: list[MaterialFile] = []
        for f in self.list_files():
            rp = self.resolve(f)
            if rp.suffix.lower() not in SUPPORTED:
                raise MaterialsError(f"arm {self.arm_id}: unsupported material type {f.name!r} (supported: "
                                     f"{sorted(SUPPORTED)}); convert or remove it so the manifest is complete")
            data = rp.read_bytes()
            files.append(MaterialFile(rp.relative_to(self.root).as_posix(), extract_text(rp),
                                      hashlib.sha256(data).hexdigest(), len(data)))
        return MaterialsBundle(self.arm_id, files)


def sandbox_for(design: Design, arm_id: str) -> ArmSandbox:
    return ArmSandbox(arm_id, design.materials_dir(arm_id))


# ---------------------------------------------------------------------------------------------------------------
# naivety check
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Finding:
    arm: str
    check: str  # ngram | word_limit | mcq_count | filename | exam_file_copy | guide_grep
    severity: str  # block | warn | allowed
    file: str
    detail: str
    evidence: str = ""
    allow_reason: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class NaivetyReport:
    findings: list[Finding] = field(default_factory=list)
    arms_checked: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def passed(self) -> bool:
        return not self.blocking

    def render(self) -> str:
        if not self.findings:
            return f"materials naivety: no findings (arms checked: {', '.join(self.arms_checked) or 'none'})"
        lines = [f"materials naivety: {len(self.blocking)} BLOCKING finding(s), "
                 f"{sum(f.severity == 'warn' for f in self.findings)} warning(s), "
                 f"{sum(f.severity == 'allowed' for f in self.findings)} allowed by design exception"]
        for f in sorted(self.findings, key=lambda x: ({"block": 0, "warn": 1, "allowed": 2}[x.severity], x.arm, x.file)):
            tag = {"block": "BLOCK", "warn": "warn ", "allowed": "allow"}[f.severity]
            lines.append(f"  [{tag}] arm {f.arm} / {f.file} / {f.check}: {f.detail}")
            if f.evidence:
                lines.append(f"          evidence: {f.evidence}")
            if f.allow_reason:
                lines.append(f"          exception reason: {f.allow_reason}")
        if self.blocking:
            lines.append("Fix or remove the flagged material (exam-taker methodology 4.3.1) or, after human review, add a "
                         "documented `naivety.allow` exception to the design. The run will not start.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "arms_checked": self.arms_checked, "findings": [f.to_dict() for f in self.findings]}


_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_FILENAME_PATTERNS = [
    (re.compile(r"answer[ _\-.]?keys?|\bkeys?\b(?=[ _\-.]*(?:\.|$))|\bkey[ _\-.]?(?:to|for)\b", re.I), "answer-key filename"),
    (re.compile(r"rubric|grading[ _\-.]?(?:guide|sheet|key)|scoring[ _\-.]?(?:guide|sheet|key)|marking[ _\-.]?(?:guide|scheme)", re.I),
     "rubric / grading-guide filename"),
    (re.compile(r"model[ _\-.]?answers?|solutions?\b", re.I), "model-answer / solutions filename"),
    (re.compile(r"exam[ _\-.]?instructions|final[ _\-.]?exam[ _\-.]?instructions|\bday[ _\-.]?of\b", re.I), "exam-instructions filename"),
]
_GUIDE_GREP = re.compile(r"final.exam.instructions|exam.id|word.limit:|word count:|fact pattern", re.I)

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen",
         "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_words(n: int) -> list[str]:
    """Spelled-out forms for 1..99 ('forty-two' and 'forty two'); [] otherwise."""
    if not 1 <= n <= 99:
        return []
    if n < 20:
        return [_ONES[n]]
    t, o = divmod(n, 10)
    return [_TENS[t]] if o == 0 else [f"{_TENS[t]}-{_ONES[o]}", f"{_TENS[t]} {_ONES[o]}"]


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("’", "'"))


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def _num_alts(n: int) -> str:
    forms = {str(n), f"{n:,}"}
    return "|".join(sorted((re.escape(f) for f in forms), key=len, reverse=True))


def _word_limit_hits(text: str, ceiling: int) -> list[tuple[str, bool]]:
    num = _num_alts(ceiling)
    pats = [
        rf"(?<![\d,.])(?:{num})(?![\d,])\s*(?:-\s*)?words?\b",
        rf"\bword[ -]?(?:limit|count|cap|ceiling|maximum|max)s?\s*(?:is|of|:|-|=)?\s*(?:of\s+)?(?:{num})(?![\d,])",
        rf"\b(?:limit(?:ed)?|capped|maximum|max|ceiling|cap)\s*(?:of|to|at|:)?\s*(?:{num})(?![\d,])\s*(?:words?)?",
    ]
    out = []
    for p in pats:
        for m in re.finditer(p, text, re.I):
            before = text[max(0, m.start() - 12):m.start()]
            in_range = bool(re.search(r"\d\s*(?:[–—-]|to)\s*$", before))
            out.append((text[max(0, m.start() - 30):m.end() + 30].replace("\n", " "), in_range))
    return out


def _mcq_count_hits(text: str, n: int) -> list[tuple[str, bool]]:
    forms = [re.escape(str(n))] + [re.escape(w) for w in number_words(n)]
    num = "|".join(forms)
    noun = r"(?:multiple[- ]choice(?:\s+(?:questions?|items?))?|mcqs?|questions?|items?)"
    pats = [rf"(?<![\d,.])(?:{num})(?![\d,])\s*[- ]?\s*{noun}\b",
            rf"\b(?:multiple[- ]choice|mcqs?)(?:\s+(?:questions?|section|items?))?\s*[:(]?\s*(?:{num})(?![\d,])"]
    out = []
    for p in pats:
        for m in re.finditer(p, text, re.I):
            before = text[max(0, m.start() - 12):m.start()]
            in_range = bool(re.search(r"\d\s*(?:[–—-]|to|or)\s*$", before))
            out.append((text[max(0, m.start() - 30):m.end() + 30].replace("\n", " "), in_range))
    return out


def check_materials(design: Design) -> NaivetyReport:
    """Scan every non-A arm's materials against this term's exam. Pure, offline, deterministic."""
    report = NaivetyReport()
    n = design.naivety.ngram
    q_tokens: dict[str, list[str]] = {c.id: _tokens(design.question_text(c)) for c in design.components}
    exam_hashes: dict[str, str] = {}
    for c in design.components:
        for rel in (c.question_file, c.answer_key_file):
            if rel and design.path(rel).is_file():
                exam_hashes[hashlib.sha256(design.path(rel).read_bytes()).hexdigest()] = rel
    q_grams = {cid: _ngrams(t, min(n, len(t))) if len(t) >= 4 else set() for cid, t in q_tokens.items()}
    q_n = {cid: min(n, len(t)) for cid, t in q_tokens.items()}

    allow = {(a.arm, a.file, a.check): a.reason for a in design.naivety.allow}

    def add(arm, check, file, detail, evidence="", severity="block"):
        reason = allow.get((arm, file, check), "")
        if reason and severity == "block":
            severity = "allowed"
        report.findings.append(Finding(arm, check, severity, file, detail, evidence, reason))

    for arm in design.arms:
        report.arms_checked.append(arm.id)
        sb = sandbox_for(design, arm.id)
        if sb.root is None:
            continue
        for f in sb.list_files():
            rel = f.relative_to(sb.root).as_posix()
            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if sha in exam_hashes:
                add(arm.id, "exam_file_copy", rel, f"byte-identical to the exam/answer-key file {exam_hashes[sha]!r}")
            for part in Path(rel).parts:
                for pat, what in _FILENAME_PATTERNS:
                    if pat.search(part):
                        add(arm.id, "filename", rel, f"{what}: {part!r}")
                        break
            try:
                text = extract_text(f)
            except ValueError:
                continue  # unsupported type is reported when the bundle is loaded
            toks = _tokens(text)
            for cid, grams in q_grams.items():
                if not grams:
                    continue
                k = q_n[cid]
                hits = [i for i in range(len(toks) - k + 1) if tuple(toks[i:i + k]) in grams]
                if hits:
                    add(arm.id, "ngram", rel,
                        f"{len(hits)} overlapping {k}-gram position(s) with the question text of component {cid!r}",
                        " ".join(toks[hits[0]:hits[0] + k]))
            for comp in design.essays():
                if comp.word_ceiling:
                    seen = set()
                    for ev, in_range in _word_limit_hits(text, comp.word_ceiling):
                        if ev in seen:
                            continue
                        seen.add(ev)
                        add(arm.id, "word_limit", rel, f"states this term's exact word limit {comp.word_ceiling} (component {comp.id})"
                            + (" inside a range" if in_range else ""), ev, "warn" if in_range else "block")
            for comp in design.mcqs():
                if comp.n_questions:
                    seen = set()
                    for ev, in_range in _mcq_count_hits(text, comp.n_questions):
                        if ev in seen:
                            continue
                        seen.add(ev)
                        add(arm.id, "mcq_count", rel, f"states this term's exact MCQ count {comp.n_questions} (component {comp.id})"
                            + (" as the end of a range (generic format language is in scope; confirm)" if in_range else ""),
                            ev, "warn" if in_range else "block")
            lines = [i + 1 for i, ln in enumerate(text.splitlines()) if _GUIDE_GREP.search(ln)]
            if lines:
                add(arm.id, "guide_grep", rel,
                    f"matches the NEXT_SESSION_GUIDE Step 1 grep on {len(lines)} line(s) (first: {lines[:5]}); general "
                    f"pre-semester syllabus language is in scope, this term's exam structure is not: human review", severity="warn")
    return report


def ensure_materials_naive(design: Design) -> NaivetyReport:
    rep = check_materials(design)
    if not rep.passed:
        raise MaterialsNaivetyError(rep)
    return rep
