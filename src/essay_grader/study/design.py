"""Study design spec (YAML): exam components, arms, models, reserved ExamIDs.

All relative paths in the file resolve against the design file's directory. Unknown keys are errors (typos in a research
design should not pass silently).

    study_id: fall-2026-quill-widgets
    protocol: protocol/                  # directory with protocol.yaml + templates (never Penn exam content in the repo)
    exam: {course: ..., professor: ..., term: "Fall 2026", date: 2026-12-10, delivery: in-class}
    section: A                           # registrar term-section letter; uniform across arms (no arm tell)
    components:
      - {id: q1, kind: essay, title: "Question 1: ...", question_file: exam/q1.txt, word_ceiling: 600}
      - {id: mcq, kind: mcq, question_file: exam/mcq.txt, n_questions: 5, answer_key_file: exam/key.txt}
    arms:
      - {id: A, materials: null}                 # no materials
      - {id: S, materials: materials/syllabus}   # syllabus only (Wave 2 default)
      - {id: B, materials: materials/full}       # with materials
    models: [{key: opus, provider: anthropic, model: claude-opus-5-5}]
    examids: {A: "0137", S: "0460", B: "0891"}   # or "A/opus": ... when there are several models
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class DesignError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExamInfo(_Strict):
    course: str
    professor: str
    term: str
    date: str | None = None  # YYYY-MM-DD
    delivery: Literal["in-class", "take-home"] = "in-class"

    @field_validator("date", mode="before")
    @classmethod
    def _date_str(cls, v):
        return None if v is None else str(v)


class Component(_Strict):
    id: str
    kind: Literal["essay", "mcq"]
    title: str | None = None
    question_file: str
    word_ceiling: int | None = Field(default=None, gt=0)  # essays; None = no numeric ceiling
    n_questions: int | None = Field(default=None, gt=0)  # mcq
    answer_key_file: str | None = None  # mcq; HUMAN-held, must never sit inside a materials folder
    answer_format: Literal["letters", "prose"] = "letters"  # mcq: letters-only vs explanations required
    points: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    target_band: tuple[float, float] = (0.85, 0.95)

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not _SAFE.match(v) or "__" in v:
            raise ValueError("component id must match [A-Za-z0-9][A-Za-z0-9_-]* and not contain '__'")
        return v

    @model_validator(mode="after")
    def _kind_fields(self):
        if self.kind == "mcq" and not self.n_questions:
            raise ValueError("mcq components need n_questions")
        if self.kind == "essay" and (self.n_questions or self.answer_key_file):
            raise ValueError("n_questions/answer_key_file apply to mcq components only")
        if self.kind == "mcq" and self.word_ceiling:
            raise ValueError("word_ceiling applies to essay components only")
        return self


class Arm(_Strict):
    id: str
    label: str | None = None
    materials: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not re.fullmatch(r"[A-Z]", v):
            raise ValueError("arm id must be a single capital letter (A, S, B, C ...)")
        return v

    @model_validator(mode="after")
    def _materials_rule(self):
        if self.id == "A" and self.materials is not None:
            raise ValueError("arm A is the no-materials arm; `materials` must be null")
        if self.id != "A" and not self.materials:
            raise ValueError(f"arm {self.id} needs a materials folder (only arm A has none)")
        return self


class ModelSpec(_Strict):
    key: str
    provider: str
    model: str
    effort: Literal["low", "medium", "high"] | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    # USD per million tokens (input, output, cache read). Needed to budget-gate a model llm.PRICE_PER_MTOK does not know.
    price_per_mtok: tuple[float, float, float | None] | None = None

    @field_validator("key")
    @classmethod
    def _key(cls, v):
        if not _SAFE.match(v) or "__" in v:
            raise ValueError("model key must match [A-Za-z0-9][A-Za-z0-9_-]* and not contain '__'")
        return v


class ExamIdPolicy(_Strict):
    source: str = ""  # e.g. "registrar-reserved"
    confirmed: bool = False  # a human confirmed with the registrar that these IDs are reserved and collision-free
    note: str = ""


class NaivetyAllow(_Strict):
    arm: str
    file: str  # path relative to the arm folder
    check: str
    reason: str = Field(min_length=10)


class NaivetyConfig(_Strict):
    ngram: int = Field(default=8, ge=3)
    allow: list[NaivetyAllow] = []


class PackageConfig(_Strict):
    timestamp_base: str | None = None  # YYYYMMDDHHMM of the first upload; others follow at 3-minute steps in seeded order
    line_spacing: Literal["onehalf", "double", "single"] = "onehalf"  # "onehalf" == exam-taker build_examplify_pdf.py
    # openpyxl writes docProps/app.xml <Application>Microsoft Excel Compatible / Openpyxl x.y</Application>, which the Wave 1
    # builder leaves in place (an AI-vs-real tell, not an arm tell). null = Wave 1 behavior; set e.g. "Microsoft Excel" to
    # rewrite it. The registrar's real value is unknown (no sample in this repo): VERIFY before using.
    xlsx_application: str | None = None


class Defaults(_Strict):
    effort: Literal["low", "medium", "high"] = "high"
    max_tokens: int = Field(default=16000, gt=0)


class Design(_Strict):
    study_id: str
    protocol: str
    exam: ExamInfo
    section: str = "A"
    components: list[Component]
    arms: list[Arm]
    models: list[ModelSpec]
    examids: dict[str, str]
    examid_policy: ExamIdPolicy = ExamIdPolicy()
    real_id_range: tuple[int, int] | None = None  # lets validate warn about the Wave 1 ID-range tell (L-005)
    seed: int = 0
    defaults: Defaults = Defaults()
    naivety: NaivetyConfig = NaivetyConfig()
    package: PackageConfig = PackageConfig()
    base_dir: Path = Field(default=Path("."), exclude=True)

    @field_validator("study_id")
    @classmethod
    def _sid(cls, v):
        if not _SAFE.match(v):
            raise ValueError("study_id must match [A-Za-z0-9][A-Za-z0-9_-]*")
        return v

    @field_validator("section")
    @classmethod
    def _section(cls, v):
        if not re.fullmatch(r"[A-Za-z]", v):
            raise ValueError("section must be one letter (the registrar's term-section code)")
        return v.upper()

    @field_validator("examids", mode="before")
    @classmethod
    def _ids_str(cls, v):
        return {str(k): str(x) for k, x in dict(v).items()}

    # ---- loading -------------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Design":
        p = Path(path).resolve()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            d = cls.model_validate(raw)
        except FileNotFoundError as e:
            raise DesignError(f"design file not found: {p}") from e
        except (ValidationError, yaml.YAMLError) as e:
            raise DesignError(f"invalid design {p.name}: {e}") from e
        d.base_dir = p.parent
        return d

    def path(self, rel: str) -> Path:
        return (self.base_dir / rel).resolve()

    # ---- structure -----------------------------------------------------------------------------------------------
    @property
    def arm_ids(self) -> list[str]:
        return [a.id for a in self.arms]

    def arm(self, arm_id: str) -> Arm:
        return next(a for a in self.arms if a.id == arm_id)

    def model(self, key: str) -> ModelSpec:
        return next(m for m in self.models if m.key == key)

    def component(self, cid: str) -> Component:
        return next(c for c in self.components if c.id == cid)

    def materials_dir(self, arm_id: str) -> Path | None:
        a = self.arm(arm_id)
        return self.path(a.materials) if a.materials else None

    def cells(self) -> list["Cell"]:
        return [Cell(c.id, a.id, m.key) for c in self.components for a in self.arms for m in self.models]

    def examid_for(self, arm_id: str, model_key: str) -> str | None:
        """`arm/model` wins over plain `arm`; plain `arm` only applies when there is exactly one model."""
        if f"{arm_id}/{model_key}" in self.examids:
            return self.examids[f"{arm_id}/{model_key}"]
        if len(self.models) == 1 and arm_id in self.examids:
            return self.examids[arm_id]
        return None

    def examid_map(self) -> dict[tuple[str, str], str]:
        return {(a.id, m.key): self.examid_for(a.id, m.key) for a in self.arms for m in self.models}  # type: ignore[misc]

    def effort_for(self, comp: Component, model: ModelSpec) -> str:
        return model.effort or self.defaults.effort

    def max_tokens_for(self, comp: Component, model: ModelSpec) -> int:
        return comp.max_tokens or model.max_tokens or self.defaults.max_tokens

    def essays(self) -> list[Component]:
        return [c for c in self.components if c.kind == "essay"]

    def mcqs(self) -> list[Component]:
        return [c for c in self.components if c.kind == "mcq"]

    def question_text(self, comp: Component) -> str:
        from ..ingest import extract_text

        return extract_text(self.path(comp.question_file))


@dataclass(frozen=True)
class Cell:
    component: str
    arm: str
    model: str

    @property
    def id(self) -> str:
        return f"{self.component}__{self.arm}__{self.model}"

    @classmethod
    def parse(cls, cell_id: str) -> "Cell":
        c, a, m = cell_id.split("__")
        return cls(c, a, m)


@dataclass
class ValidationReport:
    errors: list[str]
    warnings: list[str]
    info: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        out = [f"ERROR: {e}" for e in self.errors] + [f"WARNING: {w}" for w in self.warnings] + [f"ok: {i}" for i in self.info]
        return "\n".join(out)


def validate_design(design: Design) -> ValidationReport:
    """Static checks that need no API: files, uniqueness, folder disjointness, ExamID policy, provider registry, protocol."""
    from .protocol import Protocol, ProtocolError

    errs: list[str] = []
    warns: list[str] = []
    info: list[str] = []

    def uniq(label: str, vals: list[str]):
        dup = sorted({v for v in vals if vals.count(v) > 1})
        if dup:
            errs.append(f"duplicate {label}: {dup}")

    uniq("component ids", [c.id for c in design.components])
    uniq("arm ids", design.arm_ids)
    uniq("model keys", [m.key for m in design.models])
    if not design.components:
        errs.append("no components")
    if not design.arms:
        errs.append("no arms")
    if not design.models:
        errs.append("no models")

    # protocol
    proto = None
    try:
        proto = Protocol.load(design.path(design.protocol))
        info.append(f"protocol {proto.protocol_id} prompt_version {proto.prompt_version} (templates hash-verified)")
    except ProtocolError as e:
        errs.append(f"protocol: {e}")

    # components
    exam_files: list[Path] = []
    for c in design.components:
        q = design.path(c.question_file)
        if not q.is_file():
            errs.append(f"component {c.id}: question_file not found: {q}")
        else:
            exam_files.append(q)
        if proto and c.kind not in proto.templates:
            errs.append(f"component {c.id}: protocol has no {c.kind!r} template")
        if c.kind == "essay" and not c.word_ceiling:
            warns.append(f"component {c.id}: no word_ceiling; the audit cannot check length")
        if c.answer_key_file:
            k = design.path(c.answer_key_file)
            if k.is_file():
                exam_files.append(k)
            else:
                warns.append(f"component {c.id}: answer_key_file not found yet ({k}); needed for `study package`")
        elif c.kind == "mcq":
            warns.append(f"component {c.id}: no answer_key_file; packaging the MCQ workbook needs the instructor's key")
    if len(design.mcqs()) > 1:
        warns.append("more than one MCQ component: the registrar releases ONE MCQ workbook per exam; `study package` "
                     "will refuse until that is resolved")
    if len(design.essays()) > 2:
        warns.append("more than two essay components: the Wave 1 PDF format is verified for 1-2 essays only; "
                     "`study package` will refuse to invent a layout for more")
    if design.exam.delivery == "take-home":
        warns.append("take-home delivery: the take-home Examplify format is not implemented; ask the operator before packaging")

    # arms / materials boundary
    roots: dict[str, Path] = {}
    for a in design.arms:
        if a.materials is None:
            continue
        r = design.path(a.materials)
        if not r.is_dir():
            errs.append(f"arm {a.id}: materials folder not found: {r}")
            continue
        if not any(f.is_file() and not f.name.startswith(".") for f in r.rglob("*")):
            errs.append(f"arm {a.id}: materials folder is empty: {r}")
        roots[a.id] = r
    ids = sorted(roots)
    for i, x in enumerate(ids):
        for y in ids[i + 1:]:
            if roots[x] == roots[y] or roots[x].is_relative_to(roots[y]) or roots[y].is_relative_to(roots[x]):
                errs.append(f"materials folders of arms {x} and {y} overlap ({roots[x]} / {roots[y]}); each arm must read "
                            f"ONLY its own disjoint folder")
    for aid, r in roots.items():
        for f in exam_files:
            if f.is_relative_to(r):
                errs.append(f"arm {aid}: exam file or answer key lives inside its materials folder: {f}")
        if proto and (proto.root == r or proto.root.is_relative_to(r)):
            errs.append(f"arm {aid}: protocol directory lives inside its materials folder")
    if "A" not in design.arm_ids:
        warns.append("no A (no-materials) arm in this design")

    # models / providers
    from .providers import PROVIDER_FACTORIES
    from ..llm import price_for

    for m in design.models:
        if m.provider not in PROVIDER_FACTORIES:
            errs.append(f"model {m.key}: unknown provider {m.provider!r} (known: {sorted(PROVIDER_FACTORIES)})")
        if m.price_per_mtok is None and price_for(m.model) is None:
            warns.append(f"model {m.key} ({m.model}) has no known price: a finite --budget-usd cannot gate it. Set "
                         f"price_per_mtok in the design or the run will refuse.")

    # ExamIDs
    seen: dict[str, tuple[str, str]] = {}
    for (arm_id, mk), eid in design.examid_map().items():
        if eid is None:
            errs.append(f"no ExamID for arm {arm_id} / model {mk} (use `{arm_id}` for one model, `{arm_id}/{mk}` for several)")
            continue
        if not re.fullmatch(r"\d{1,6}", eid):
            errs.append(f"ExamID {eid!r} (arm {arm_id}/{mk}) must be digits only (the MCQ workbook stores it as an integer)")
        if eid in seen:
            errs.append(f"ExamID {eid} is used by both {seen[eid]} and {(arm_id, mk)}; each cell group needs its own")
        seen[eid] = (arm_id, mk)
    known = {f"{a}/{m}" for a in design.arm_ids for m in (x.key for x in design.models)} | set(design.arm_ids)
    for k in design.examids:
        if k not in known:
            errs.append(f"examids key {k!r} matches no arm or arm/model")
    if not design.examid_policy.confirmed:
        warns.append("examid_policy.confirmed is false: `study package` will refuse until a human confirms the IDs are "
                     "registrar-reserved and collision-free (exam-taker CLAUDE.md: do not invent IDs)")
    if design.real_id_range and seen:
        lo, hi = design.real_id_range
        nums = sorted(int(e) for e in seen if e.isdigit())
        outside = [n for n in nums if n > hi or n < lo]
        if outside:
            warns.append(f"ExamIDs {outside} lie outside the real-student range {lo}-{hi}: a visible tell (Wave 1 L-005)")
        elif len(nums) > 1 and (nums[-1] - nums[0]) < 0.1 * (hi - lo):
            warns.append(f"ExamIDs {nums} are clustered within 10% of the real range: a visible tell (Wave 1 L-005)")
    elif not design.real_id_range:
        info.append("real_id_range not set: ID-range tell (L-005) not checked")

    info.append(f"{len(design.cells())} cells = {len(design.components)} components x {len(design.arms)} arms x {len(design.models)} models")
    return ValidationReport(errs, warns, info)
