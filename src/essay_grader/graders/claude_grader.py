"""Claude grader: per-criterion points/level, verbatim evidence (validated), rationale, confidence."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..llm import DEFAULT_CLAUDE_MODEL, StructuredLLM, with_retries
from ..prompts import GRADER_SYSTEM, GRADER_USER, render_question
from ..rubric import Question, Rubric
from .base import CriterionGrade, GraderResult


class CriterionJudgment(BaseModel):
    criterion_id: str
    met: bool | None = Field(default=None, description="binary criteria only")
    level: str | None = Field(default=None, description="choice criteria only: exact level label")
    score: float | None = Field(default=None, description="score criteria only")
    evidence: list[str] = Field(default_factory=list, description="verbatim substrings of the student answer")
    rationale: str
    confidence: float = Field(ge=0, le=1)


class GradeOutput(BaseModel):
    judgments: list[CriterionJudgment]


_SUBS = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}


def _norm(s: str) -> str:
    for k, v in _SUBS.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s).strip().casefold()


def validate_quotes(answer: str, quotes: list[str]) -> list[str]:
    """Return the quotes that are NOT substrings of the answer (whitespace/case/smart-quote insensitive).

    A quote may use '...' or an ellipsis to skip text; each fragment must appear.
    """
    hay = _norm(answer)
    bad = []
    for q in quotes:
        frags = [f for f in re.split(r"\.\.\.|…", q) if _norm(f)]
        if not frags or any(_norm(f) not in hay for f in frags):
            bad.append(q)
    return bad


class ClaudeGrader:
    name = "claude"

    def __init__(self, llm: StructuredLLM, *, allow_draft: bool = False, retry_delay: float = 1.0):
        self.llm, self.allow_draft, self.retry_delay = llm, allow_draft, retry_delay

    @property
    def model(self) -> str:
        return self.llm.model

    async def grade(self, rubric: Rubric, question: Question, exam_text: str, answer_text: str) -> GraderResult:
        if not self.allow_draft:
            rubric.require_locked()
        prefix = GRADER_SYSTEM.format(exam=exam_text, question_id=question.id, rubric=render_question(rubric, question))
        user = GRADER_USER.format(answer=answer_text)
        hint = {"kind": "grade", "question": question.model_dump(), "answer": answer_text}

        async def once() -> GraderResult:
            res = await self.llm.call(prefix=prefix, user=user, tool_name="submit_grades",
                                      tool_description="Submit per-criterion suggested grades.",
                                      schema=GradeOutput, max_tokens=4096, hint=hint)
            out: GradeOutput = res.parsed  # type: ignore[assignment]
            return GraderResult(grader=self.name, model=self.model, served_model=res.served_model,
                                question_id=question.id, grades=self._convert(question, out, answer_text),
                                usage=res.usage.to_dict())

        return await with_retries(once, attempts=2, base_delay=self.retry_delay, retry_on=(ValueError, KeyError))

    @staticmethod
    def _convert(question: Question, out: GradeOutput, answer_text: str) -> list[CriterionGrade]:
        by_id = {c.id: c for c in question.criteria}
        got = [j.criterion_id for j in out.judgments]
        if sorted(got) != sorted(by_id):
            raise ValueError(f"criterion ids mismatch: expected {sorted(by_id)}, got {sorted(got)}")
        grades = []
        for j in out.judgments:
            c = by_id[j.criterion_id]
            pts = c.points_for(met=j.met, level=j.level, score=j.score)
            label = ("met" if j.met else "not_met") if c.kind == "binary" else j.level if c.kind == "choice" else None
            grades.append(CriterionGrade(
                criterion_id=c.id, points=pts, label=label, score=j.score if c.kind == "score" else None,
                evidence=j.evidence, rationale=j.rationale, confidence=j.confidence,
                invalid_quotes=validate_quotes(answer_text, j.evidence)))
        return grades
