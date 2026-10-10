"""Common grader types."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from ..rubric import Question, Rubric


class CriterionGrade(BaseModel):
    criterion_id: str
    points: float
    label: str | None = None  # "met"/"not_met" (binary) or level label (choice)
    score: float | None = None  # raw score for score criteria
    probability: float | None = None  # decisions predicate probability
    evidence: list[str] = Field(default_factory=list)
    rationale: str = ""
    confidence: float | None = None
    probabilities: list[dict] | None = None  # Decisions choice/score per-level probabilities
    invalid_quotes: list[str] = Field(default_factory=list)

    @property
    def quotes_valid(self) -> bool:
        return not self.invalid_quotes


class GraderResult(BaseModel):
    grader: str
    model: str
    served_model: str | None = None
    question_id: str
    grades: list[CriterionGrade] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    error: str | None = None


class Grader(Protocol):
    name: str
    model: str

    async def grade(self, rubric: Rubric, question: Question, exam_text: str, answer_text: str, *,
                    budget=None) -> GraderResult: ...
