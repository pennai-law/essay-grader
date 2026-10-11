"""Rubric schema, loading/saving, and locking (frozen copy + sha256 content hash)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class Level(BaseModel):
    label: str
    points: float
    descriptor: str


class Anchor(BaseModel):
    value: float
    descriptor: str


class Scale(BaseModel):
    min: float = 0
    max: float
    anchors: list[Anchor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "Scale":
        if self.max <= self.min:
            raise ValueError("scale.max must be greater than scale.min")
        for a in self.anchors:
            if not (self.min <= a.value <= self.max):
                raise ValueError(f"anchor {a.value} outside scale [{self.min}, {self.max}]")
        return self


class Criterion(BaseModel):
    id: str
    description: str = Field(description="Observable behavior in the answer that earns the points.")
    max_points: float = Field(gt=0)
    kind: Literal["binary", "choice", "score"]
    levels: list[Level] = Field(default_factory=list)
    scale: Scale | None = None
    notes: str | None = None
    edge_case_rulings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "Criterion":
        if self.kind == "choice":
            if len(self.levels) < 2:
                raise ValueError(f"{self.id}: choice criteria need at least 2 levels")
            labels = [lv.label for lv in self.levels]
            if len(set(labels)) != len(labels):
                raise ValueError(f"{self.id}: duplicate level labels")
            if any(lv.points < 0 or lv.points > self.max_points for lv in self.levels):
                raise ValueError(f"{self.id}: level points must be within 0..max_points")
        elif self.kind == "score" and self.scale is None:
            raise ValueError(f"{self.id}: score criteria need a scale")
        return self

    def points_for(self, *, met: bool | None = None, level: str | None = None, score: float | None = None) -> float:
        """Normalize a raw judgment to points."""
        if self.kind == "binary":
            if met is None:
                raise ValueError(f"{self.id}: binary criterion needs `met`")
            return self.max_points if met else 0.0
        if self.kind == "choice":
            for lv in self.levels:
                if lv.label == level:
                    return lv.points
            raise ValueError(f"{self.id}: unknown level {level!r}")
        assert self.scale is not None
        if score is None:
            raise ValueError(f"{self.id}: score criterion needs `score`")
        s = self.scale
        if not (s.min - 1e-9 <= score <= s.max + 1e-9):
            raise ValueError(f"{self.id}: score {score} outside [{s.min}, {s.max}]")
        return (score - s.min) / (s.max - s.min) * self.max_points


class Question(BaseModel):
    id: str
    prompt: str | None = Field(default=None, description="Optional short restatement of the question.")
    criteria: list[Criterion]

    @model_validator(mode="after")
    def _check(self) -> "Question":
        ids = [c.id for c in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError(f"question {self.id}: duplicate criterion ids")
        if not ids:
            raise ValueError(f"question {self.id}: no criteria")
        return self

    @property
    def max_points(self) -> float:
        return sum(c.max_points for c in self.criteria)


class LengthPolicy(BaseModel):
    mode: Literal["no_penalty", "grade_first_n_words", "deduct"] = "no_penalty"
    word_cap: int | None = Field(default=None, gt=0)
    deduct_points_per_100_words: float = 0.0  # used by mode == "deduct"
    max_deduction: float | None = None

    @model_validator(mode="after")
    def _check(self) -> "LengthPolicy":
        if self.mode != "no_penalty" and self.word_cap is None:
            raise ValueError("length_policy.word_cap is required unless mode is no_penalty")
        return self


class Policies(BaseModel):
    length_policy: LengthPolicy = Field(default_factory=LengthPolicy)
    off_topic_policy: str = "Do not award points for material unrelated to the question; do not penalize it either unless a criterion says so."
    alternative_conclusions_policy: str = "Credit well-reasoned conclusions that differ from the model answer; grade the reasoning, not the conclusion."
    citation_policy: str = "Accurate citation of authority is credited only where a criterion asks for it; do not reward citation volume."
    additional_rulings: list[str] = Field(default_factory=list, description="Rubric-wide rulings (from questionnaires).")


class Calibration(BaseModel):
    sample_ids: list[str] = Field(default_factory=list)
    parent_version: int | None = None
    parent_hash: str | None = None
    model: str | None = None


class Provenance(BaseModel):
    """Where an applied ruling/rewrite came from: which hardening (or calibration) run and which question."""
    hardening_run_id: str
    question_id: str
    run_kind: Literal["hardening", "calibration"] = "hardening"
    criterion_id: str | None = None
    action: Literal["ruling", "rewrite_descriptor", "no_change"] = "ruling"
    target: str | None = None  # rewrite target, e.g. "description", "level:adequate"
    text: str
    before: str | None = None  # previous text for rewrites
    option_label: str | None = None
    source: Literal["answer", "recommended_default", "custom"]
    applied_at: str | None = None


class Rubric(BaseModel):
    exam_id: str
    version: int = 1
    questions: list[Question]
    policies: Policies = Field(default_factory=Policies)
    calibration: Calibration | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    locked: bool = False
    content_hash: str | None = None
    locked_at: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Rubric":
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate question ids")
        if not ids:
            raise ValueError("rubric has no questions")
        return self

    def question(self, qid: str) -> Question:
        for q in self.questions:
            if q.id == qid:
                return q
        raise KeyError(f"unknown question id {qid!r}")

    def compute_hash(self) -> str:
        """sha256 over canonical JSON of the rubric content (excludes lock bookkeeping fields)."""
        data = self.model_dump(mode="json", exclude={"locked", "content_hash", "locked_at"})
        # Fields added later are omitted while empty so hashes of rubrics locked before they existed stay valid.
        if not data.get("provenance"):
            data.pop("provenance", None)
        if not data["policies"].get("additional_rulings"):
            data["policies"].pop("additional_rulings", None)
        blob = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def lock(self) -> "Rubric":
        """Return a frozen copy carrying its content hash."""
        r = self.model_copy(deep=True)
        r.locked = True
        r.content_hash = r.compute_hash()
        r.locked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return r

    def is_validly_locked(self) -> bool:
        return self.locked and self.content_hash == self.compute_hash()

    def require_locked(self) -> None:
        if not self.locked:
            raise RubricNotLocked("Rubric is not locked. Run `essay-grader lock <rubric.yaml>` first.")
        if self.content_hash != self.compute_hash():
            raise RubricNotLocked("Rubric content does not match its recorded hash (edited after locking?). Re-lock it.")

    def with_rulings(self, rulings: list[tuple[str, str]], *, calibration: Calibration | None = None) -> "Rubric":
        """New *unlocked* version with (criterion_id, ruling_text) rulings appended. Version bumped."""
        r = self.model_copy(deep=True)
        by_id = {c.id: c for q in r.questions for c in q.criteria}
        for cid, text in rulings:
            if cid not in by_id:
                raise KeyError(f"unknown criterion id {cid!r}")
            by_id[cid].edge_case_rulings.append(text)
        r.version = self.version + 1
        r.locked, r.content_hash, r.locked_at = False, None, None
        if calibration is not None:
            r.calibration = calibration
        return r


class RubricNotLocked(RuntimeError):
    pass


def load_rubric(path: str | Path) -> Rubric:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)
    return Rubric.model_validate(data)


def save_rubric(rubric: Rubric, path: str | Path) -> Path:
    p = Path(path)
    data = rubric.model_dump(mode="json", exclude_none=True)
    if p.suffix == ".json":
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")
    return p


def locked_path_for(path: str | Path) -> Path:
    p = Path(path)
    stem = p.stem[:-7] if p.stem.endswith(".locked") else p.stem
    return p.with_name(f"{stem}.locked{p.suffix or '.yaml'}")
