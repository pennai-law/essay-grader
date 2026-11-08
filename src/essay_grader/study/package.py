"""Packaging: Examplify-format PDFs (essays) and the registrar-style MCQ workbook (xlsx).

PORTED from exam-taker `_shared_tooling/build_examplify_pdf.py` and `build_examplify_mcq_xlsx.py` (no runtime import from
that repo). The LaTeX source is byte-for-byte what the Wave 1 script generates (see tests/fixtures/study/golden); the only
additions are (a) an optional `line_spacing` knob whose default reproduces the script, and (b) the guards below.

PDF: xelatex (TeX Live basic) with system Helvetica, run twice so `\\pageref{LastPageOfExam}` resolves. If xelatex is absent
`package_study` still writes the .tex sources and reports the PDFs as not built; Docker needs texlive-xetex (or texlive basic
+ xetex + fontspec + fancyhdr + setspace) and a Helvetica-compatible font, see docs/STUDY.md.

No arm tells: the arm is never an input to the PDF, filename or workbook. The section letter is the registrar's term-section
code, uniform across arms (exam-taker decision-log 2026-05-03). The arm <-> ExamID map is written to
`package/manifest.private.json`, outside `deliver/`.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import random
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from .audit import LEAK_RE, audit_text
from .design import Cell, Design
from .protocol import Protocol
from .store import StudyStore


class PackagingError(Exception):
    pass


class XelatexMissing(PackagingError):
    pass


# ---------------------------------------------------------------------------------------------------------------
# PDF: ported verbatim from build_examplify_pdf.py
# ---------------------------------------------------------------------------------------------------------------
def latex_escape(text: str) -> str:
    """Escape LaTeX special characters in body text. xelatex handles unicode natively."""
    replacements = [
        ("\\", r"\textbackslash{}"), ("{", r"\{"), ("}", r"\}"), ("$", r"\$"), ("%", r"\%"), ("&", r"\&"),
        ("#", r"\#"), ("_", r"\_"), ("^", r"\^{}"), ("~", r"\~{}"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def count_words(text: str) -> int:
    return len(text.split())


def count_chars_with_whitespace(text: str) -> int:
    return len(text)


def count_chars_no_whitespace(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def md_paragraphs_to_latex(md_text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", md_text.strip()) if p.strip()]
    out = []
    for p in paragraphs:
        escaped = latex_escape(p)
        escaped = escaped.replace("\n", " \\\\\n")
        out.append(escaped)
    return "\n\n".join(out)


def derive_year(term: str) -> str:
    m = re.search(r"\b(?:19|20)\d{2}\b", term)
    return m.group(0) if m else "????"


def derive_exam_name(course: str, professor: str, year: str, section: str) -> str:
    """Examplify exam-name string: course_(prof)_year+section-letter. The section letter is the registrar's term-section code,
    NOT the experimental arm (conflating them produced the 2026-05-03 cross-cell tell in Wave 1)."""
    return f"{course.replace(' ', '_')}_({professor})_{year}{section.upper()}"


_SPACING = {"onehalf": r"\onehalfspacing", "double": r"\doublespacing", "single": r"\singlespacing"}


def build_latex(exam_id: str, exam_name: str, q1_title: str, q1_text: str, q2_title: str, q2_text: str, total_words: int,
                total_chars_with_ws: int, total_chars_no_ws: int, line_spacing: str = "onehalf") -> str:
    exam_name_esc = latex_escape(exam_name)
    q1_title_esc = latex_escape(q1_title) if q1_title else ""
    q2_title_esc = latex_escape(q2_title) if q2_title else ""
    exam_id_esc = latex_escape(exam_id)

    if q1_title_esc:
        q1_section = "1)\n\n" + r"\underline{" + q1_title_esc + "}\n\n" + q1_text
    else:
        q1_section = "1)\n\n" + q1_text

    q2_section = ""
    if q2_text:
        if q2_title_esc:
            q2_section = "\n\n2)\n\n" + r"\underline{" + q2_title_esc + "}\n\n" + q2_text
        else:
            q2_section = "\n\n2)\n\n" + q2_text

    return r"""\documentclass[11pt]{article}
\usepackage[margin=1in,top=1.1in,bottom=1.1in,headheight=14pt,headsep=10pt,footskip=30pt]{geometry}
\usepackage{fancyhdr}
\usepackage{setspace}
\usepackage{fontspec}
\usepackage{array}
\setmainfont{Helvetica}

\AtEndDocument{\label{LastPageOfExam}}

% ---- Header / footer with horizontal rules ----
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{""" + exam_name_esc + r"""}
\fancyhead[R]{ID: """ + exam_id_esc + r"""}
\fancyfoot[C]{\thepage{} of \pageref{LastPageOfExam}}
\renewcommand{\headrulewidth}{0.4pt}
\renewcommand{\footrulewidth}{0.4pt}

""" + _SPACING[line_spacing] + r"""
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.8em}
\raggedright

\begin{document}

% ---- Cover page ----
\thispagestyle{fancy}

\begin{center}
{\large\bfseries University of Pennsylvania Law School}
\end{center}

\vspace{0.6em}

\noindent\begin{tabular}{@{}p{1.6in}@{}p{4.5in}@{}}
\textbf{ID:} & {\Huge\bfseries """ + exam_id_esc + r"""} \quad {\small\textit{(Exam \#)}} \\[0.6em]
Exam Name: & """ + exam_name_esc + r""" \\[0.4em]
GRADE & \rule{2.5in}{0.4pt} \\
\end{tabular}

\vspace{2em}

\noindent\small Total Number of Words in this Exam = """ + str(total_words) + r"""\\
Total Number of Characters in this Exam = """ + str(total_chars_with_ws) + r"""\\
Total Number of Characters in this Exam (No Spaces, No Returns) = """ + str(total_chars_no_ws) + r"""

\normalsize
\newpage

% ---- Q1 ----
""" + q1_section + r"""

% ---- Q2 (inline, no page break) ----
""" + q2_section + r"""

\end{document}
"""


def tex_for(exam_id: str, course: str, professor: str, term: str, section: str, essays: list[tuple[str, str]],
            line_spacing: str = "onehalf") -> tuple[str, str]:
    """(tex_source, exam_name) for 1 or 2 essays given as (title, markdown_text). Raises for more: the Wave 1 layout is only
    verified for Q1 + Q2 and this port does not invent a third-question layout."""
    if not 1 <= len(essays) <= 2:
        raise PackagingError(f"the verified Examplify layout covers 1-2 essay questions, got {len(essays)}; the format for more "
                             f"is unverified (a format mismatch is a detection vector, L-040). Get a registrar sample first.")
    (t1, q1_raw), (t2, q2_raw) = essays[0], (essays[1] if len(essays) > 1 else ("", ""))
    total_words = count_words(q1_raw) + count_words(q2_raw)
    total_cws = count_chars_with_whitespace(q1_raw) + count_chars_with_whitespace(q2_raw)
    total_cnws = count_chars_no_whitespace(q1_raw) + count_chars_no_whitespace(q2_raw)
    exam_name = derive_exam_name(course, professor, derive_year(term), section)
    q1_latex = md_paragraphs_to_latex(q1_raw)
    q2_latex = md_paragraphs_to_latex(q2_raw) if q2_raw else ""
    q2_title_eff = t2 if q2_raw else ""
    return build_latex(exam_id, exam_name, t1, q1_latex, q2_title_eff, q2_latex, total_words, total_cws, total_cnws,
                       line_spacing), exam_name


def examplify_pdf_filename(exam_id: str, exam_name: str, timestamp: str) -> str:
    if not re.fullmatch(r"\d{12}", timestamp):
        raise PackagingError(f"timestamp must be 12 digits YYYYMMDDHHMM, got {timestamp!r}")
    return f"{exam_id}-{exam_name}_{timestamp}.pdf"


def xelatex_available() -> bool:
    return shutil.which("xelatex") is not None


def compile_pdf(tex_source: str, exam_id: str) -> bytes:
    if not xelatex_available():
        raise XelatexMissing("xelatex not found on PATH (needs TeX Live basic + xetex/fontspec/fancyhdr/setspace and Helvetica)")
    with TemporaryDirectory() as td:
        tex_path = Path(td) / "exam.tex"
        tex_path.write_text(tex_source, encoding="utf-8")
        for _ in range(2):  # twice so \pageref{LastPageOfExam} resolves
            r = subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error", str(tex_path)], cwd=td,
                               capture_output=True, text=True)
            if r.returncode != 0:
                log = Path(td) / "exam.log"
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-40:]) if log.exists() else r.stdout + r.stderr
                raise PackagingError(f"xelatex failed for ExamID {exam_id}:\n{tail}")
        return (Path(td) / "exam.pdf").read_bytes()


# ---------------------------------------------------------------------------------------------------------------
# MCQ workbook: ported from build_examplify_mcq_xlsx.py
# ---------------------------------------------------------------------------------------------------------------
KEY_RE = re.compile(r"^\s*Q\s*(\d+)\s*[:.\)]\s*([A-Ea-e])\s*$")
SUBMISSION_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*([A-Ea-e])\s*$")


def parse_answer_key(text: str) -> dict[int, str]:
    """`Qn: X` per line, or one comma-separated line of letters (question numbers 1..N)."""
    key: dict[int, str] = {}
    for line in text.splitlines():
        m = KEY_RE.match(line)
        if m:
            n, letter = int(m.group(1)), m.group(2).upper()
            if n in key and key[n] != letter:
                raise PackagingError(f"answer key has conflicting entries for Q{n}: {key[n]} vs {letter}")
            key[n] = letter
    if not key and "," in text:
        letters = [s.strip().upper() for s in text.replace("\n", ",").split(",") if s.strip()]
        for i, ltr in enumerate(letters, 1):
            if not re.fullmatch(r"[A-E]", ltr):
                raise PackagingError(f"answer-key entry {i} is not a single A-E letter: {ltr!r}")
        key = {i: ltr for i, ltr in enumerate(letters, 1)}
    return key


def parse_submission(text: str) -> dict[int, str]:
    answers: dict[int, str] = {}
    for line in text.splitlines():
        m = SUBMISSION_RE.match(line)
        if m:
            answers[int(m.group(1))] = m.group(2).upper()
    return answers


def mcq_sheet_name(course: str, professor: str) -> str:
    return f"ET_Results_{course.replace(' ', '_')}_({professor})_P"[:31]


def mcq_filename(course: str, professor: str, year: str) -> str:
    return f"MC_Results_{course.replace(' ', '_')}_({professor})_{year}.xlsx"


def _validate_contiguous(label: str, mapping: dict[int, str], n: int) -> None:
    if sorted(mapping) != list(range(1, n + 1)):
        missing = [q for q in range(1, n + 1) if q not in mapping]
        extra = [q for q in sorted(mapping) if q not in range(1, n + 1)]
        raise PackagingError(f"{label}: expected questions 1..{n}" + (f" (missing {missing})" if missing else "")
                             + (f" (extra {extra})" if extra else ""))


def build_workbook(course: str, professor: str, key: dict[int, str], submissions: list[tuple[int, dict[int, str]]]):
    """Registrar-shaped workbook. Visual-parity rules (exam-taker decision-log 2026-04-28): Aptos Narrow via replacement of
    font index 0 (so unstyled blank Pts cells in the key row inherit it); no freeze panes; creator/lastModifiedBy empty.
    Row 1 headers; row 2 'ANSWER KEY -->' with blank Pts cells as the key sentinel; rows 3+ one per submission, letter UPPER
    if correct / lower if wrong, plus a 0/1 Pts column, and the total in column B."""
    import openpyxl
    from openpyxl.styles import Font

    n = max(key)
    _validate_contiguous("answer key", key, n)
    for exam_id, answers in submissions:
        _validate_contiguous(f"submission {exam_id}", answers, n)
    wb = openpyxl.Workbook()
    wb._fonts[0] = Font(name="Aptos Narrow", size=11)
    wb.properties.creator = ""
    wb.properties.lastModifiedBy = ""
    ws = wb.active
    ws.title = mcq_sheet_name(course, professor)
    header = ["Exam #", "Pts"]
    for i in range(1, n + 1):
        header.extend([f"Q{i}", f"Q{i} Pts"])
    ws.append(header)
    key_row: list = ["ANSWER KEY -->", None]
    for i in range(1, n + 1):
        key_row.extend([key[i], None])
    ws.append(key_row)
    totals: list[tuple[int, int]] = []
    for exam_id, answers in submissions:
        total, row = 0, [exam_id, 0]
        for i in range(1, n + 1):
            student = answers[i]
            correct = student == key[i]
            row.extend([student.upper() if correct else student.lower(), 1 if correct else 0])
            total += correct
        row[1] = total
        ws.append(row)
        totals.append((exam_id, total))
    return wb, totals


def set_xlsx_application(path: Path, name: str) -> None:
    """Rewrite <Application> in docProps/app.xml in place (opt-in; see PackageConfig.xlsx_application)."""
    import zipfile

    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "docProps/app.xml":
                data = re.sub(rb"<Application>.*?</Application>", b"<Application>" + name.encode() + b"</Application>", data, flags=re.S)
            zout.writestr(item, data)
    tmp.replace(path)


# ---------------------------------------------------------------------------------------------------------------
# delivered-artifact gates
# ---------------------------------------------------------------------------------------------------------------
def pdf_text(pdf: Path) -> str | None:
    if shutil.which("pdftotext") is None:
        return None
    r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def section_consistency(names: list[str], expected: str) -> list[str]:
    """Every deliverable must carry the same {Year}{Section} code (exam-taker Step 8)."""
    bad = []
    for n in names:
        m = re.search(r"(?:19|20)\d{2}([A-Z])", n)
        if not m or m.group(1) != expected:
            bad.append(n)
    return bad


@dataclass
class PackageResult:
    deliver_dir: Path
    files: list[Path] = field(default_factory=list)
    tex_files: list[Path] = field(default_factory=list)
    pdfs_built: bool = False
    notes: list[str] = field(default_factory=list)
    mcq_totals: dict[str, int] = field(default_factory=dict)


def _timestamps(design: Design, ids: list[str]) -> dict[str, str]:
    base = design.package.timestamp_base or _dt.datetime.now().strftime("%Y%m%d%H%M")
    if not re.fullmatch(r"\d{12}", base):
        raise PackagingError(f"package.timestamp_base must be 12 digits YYYYMMDDHHMM, got {base!r}")
    t0 = _dt.datetime.strptime(base, "%Y%m%d%H%M")
    order = sorted(ids)
    random.Random(f"{design.seed}:{design.study_id}").shuffle(order)  # seeded: upload order does not reveal arm order
    return {eid: (t0 + _dt.timedelta(minutes=3 * i)).strftime("%Y%m%d%H%M") for i, eid in enumerate(order)}


def package_study(design: Design, store: StudyStore, protocol: Protocol, *, allow_unrevised_violations: bool = False,
                  build_pdfs: bool = True, citations=False) -> PackageResult:
    if design.exam.delivery != "in-class":
        raise PackagingError("take-home delivery: the take-home Examplify format differs and is not implemented; ask the "
                             "operator before generating (exam-taker CLAUDE.md invariant)")
    if not design.examid_policy.confirmed:
        raise PackagingError("examid_policy.confirmed is false: a human must confirm the ExamIDs are registrar-reserved and "
                             "collision-free before anything is packaged (exam-taker invariant: do not invent IDs)")
    if len(design.mcqs()) > 1:
        raise PackagingError("more than one MCQ component: the registrar releases one MCQ workbook per exam; resolve the layout first")
    missing = [c.id for c in design.cells() if not store.is_captured(c)]
    if missing:
        raise PackagingError(f"cells not captured yet: {missing}")

    # source text per cell: the revised pass when it exists, else the verbatim capture; either must pass the audit
    texts: dict[str, str] = {}
    sources: dict[str, str] = {}
    for cell in design.cells():
        comp = design.component(cell.component)
        text, src = store.final_text(cell)
        res = audit_text(text, comp, banned=protocol.banned_phrases(comp.kind), cell_id=cell.id, citations=citations,
                         truncated=bool(store.read_meta(cell).get("truncated")) and src == "capture")
        if not res.passed and not allow_unrevised_violations:
            raise PackagingError(f"cell {cell.id} ({src}) fails the audit: " + "; ".join(f"{f.check}: {f.message}" for f in res.violations[:5])
                                 + ". Run `study revise` (and/or fix by hand in a recorded pass) before packaging; build from the "
                                   "revised pass, never the first-pass capture.")
        if LEAK_RE.search(text):
            raise PackagingError(f"cell {cell.id}: L-043 materials leak in the text to be packaged")
        texts[cell.id], sources[cell.id] = text, src

    pkg = store.d("package")
    deliver = pkg / "deliver"
    if deliver.exists():
        shutil.rmtree(deliver)
    deliver.mkdir(parents=True)
    result = PackageResult(deliver)
    year = derive_year(design.exam.term)
    examids = design.examid_map()
    ids = [examids[(a.id, m.key)] for a in design.arms for m in design.models]
    ts = _timestamps(design, ids)
    private: dict = {"study_id": design.study_id, "note": "PRIVATE: maps deliverables to arm/model. Never ship with deliver/.",
                     "deliverables": [], "source_text": sources}
    tex_dir = pkg / "tex_private"
    tex_dir.mkdir(exist_ok=True)

    essays = design.essays()
    if essays:
        for arm in design.arms:
            for model in design.models:
                eid = examids[(arm.id, model.key)]
                parts = [(c.title or f"Question {i}", texts[Cell(c.id, arm.id, model.key).id]) for i, c in enumerate(essays, 1)]
                tex, exam_name = tex_for(eid, design.exam.course, design.exam.professor, design.exam.term, design.section, parts,
                                         design.package.line_spacing)
                fname = examplify_pdf_filename(eid, exam_name, ts[eid])
                tex_path = tex_dir / (fname[:-4] + ".tex")
                tex_path.write_text(tex, encoding="utf-8")
                result.tex_files.append(tex_path)
                private["deliverables"].append({"file": fname, "examid": eid, "arm": arm.id, "model_key": model.key, "kind": "essay-pdf",
                                                "cells": [Cell(c.id, arm.id, model.key).id for c in essays]})
                if build_pdfs and xelatex_available():
                    pdf = deliver / fname
                    pdf.write_bytes(compile_pdf(tex, eid))
                    gate = pdf_text(pdf)
                    if gate is not None and LEAK_RE.search(gate):
                        pdf.unlink()
                        raise PackagingError(f"L-043 delivered-artifact gate: leak found in the extracted text of {fname}")
                    result.files.append(pdf)
        result.pdfs_built = bool(result.files)
        if not result.pdfs_built:
            result.notes.append("xelatex not available (or build_pdfs=False): .tex sources written under package/tex_private/, no PDFs "
                                "built. Install TeX Live basic + xetex and Helvetica (see docs/STUDY.md) and re-run `study package`.")

    if design.mcqs():
        comp = design.mcqs()[0]
        if not comp.answer_key_file or not design.path(comp.answer_key_file).is_file():
            raise PackagingError(f"MCQ component {comp.id}: the instructor's answer key is needed to build the workbook "
                                 f"(answer_key_file). Without it follow the human placeholder-score contingency (methodology 6.2); "
                                 f"this tool does not fabricate scores.")
        key = parse_answer_key(design.path(comp.answer_key_file).read_text(encoding="utf-8"))
        if not key:
            raise PackagingError(f"no answers parsed from {comp.answer_key_file}")
        subs = []
        for arm in design.arms:
            for model in design.models:
                eid = examids[(arm.id, model.key)]
                answers = parse_submission(texts[Cell(comp.id, arm.id, model.key).id])
                subs.append((int(eid), answers))
                private["deliverables"].append({"file": mcq_filename(design.exam.course, design.exam.professor, year), "examid": eid,
                                                "arm": arm.id, "model_key": model.key, "kind": "mcq-xlsx-row",
                                                "cells": [Cell(comp.id, arm.id, model.key).id]})
        wb, totals = build_workbook(design.exam.course, design.exam.professor, key, subs)
        path = deliver / mcq_filename(design.exam.course, design.exam.professor, year)
        wb.save(str(path))
        if design.package.xlsx_application:
            set_xlsx_application(path, design.package.xlsx_application)
        result.files.append(path)
        result.mcq_totals = {str(e): t for e, t in totals}

    bad = section_consistency([p.name for p in result.files if p.suffix == ".pdf"] + [p.name for p in result.tex_files], design.section)
    if bad:
        raise PackagingError(f"term-section code mismatch in {bad}: every deliverable must read {year}{design.section}")
    (pkg / "manifest.private.json").write_text(json.dumps(private, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    result.deliver_dir = deliver
    return result
