"""Adapter for OpenAI's Decisions API (POST /v1/decisions).

UNVERIFIED: the request/response JSON shape below is reconstructed from third-party descriptions
(one input + an ordered list of questions of type predicate / choice / score; answers returned in the order asked).
Official docs were unreachable when this was written. Confirm against
https://developers.openai.com/api/reference/resources/decisions/methods/create
and then edit ONLY `build_request` and `parse_response` (tests in tests/test_decisions.py pin the assumed shape).
"""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from ..llm import Usage, with_retries
from ..rubric import Criterion, Question, Rubric
from .base import CriterionGrade, GraderResult

DEFAULT_DECISIONS_MODEL = "gpt-6-luna"
Transport = Callable[[str, dict], Awaitable[dict]]


def _criterion_text(c: Criterion) -> str:
    extra = "".join(f" RULING: {r}" for r in c.edge_case_rulings)
    return c.description + extra


def build_request(rubric: Rubric, question: Question, answer_text: str, model: str = DEFAULT_DECISIONS_MODEL,
                  exam_text: str = "") -> dict:
    # UNVERIFIED: confirm against https://developers.openai.com/api/reference/resources/decisions/methods/create
    questions: list[dict[str, Any]] = []
    for c in question.criteria:
        if c.kind == "binary":
            questions.append({"id": c.id, "type": "predicate", "text": _criterion_text(c)})
        elif c.kind == "choice":
            questions.append({"id": c.id, "type": "choice", "text": _criterion_text(c),
                              "options": [{"value": lv.label, "description": lv.descriptor} for lv in c.levels]})
        else:
            assert c.scale is not None
            questions.append({"id": c.id, "type": "score", "text": _criterion_text(c),
                              "min": c.scale.min, "max": c.scale.max,
                              "anchors": [{"value": a.value, "description": a.descriptor} for a in c.scale.anchors]})
    p = rubric.policies
    return {
        "model": model,
        "input": answer_text,
        "instructions": ("Evaluate this law-school essay answer against each question. Judge only the text. "
                         + (f"Exam question:\n{exam_text}\n" if exam_text else "")
                         + f"Alternative conclusions: {p.alternative_conclusions_policy} Off-topic: {p.off_topic_policy}"),
        "questions": questions,
    }


def parse_response(resp: dict, question: Question, *, threshold: float = 0.5) -> tuple[list[CriterionGrade], dict]:
    # UNVERIFIED: confirm against https://developers.openai.com/api/reference/resources/decisions/methods/create
    answers = resp.get("answers")
    if not isinstance(answers, list) or len(answers) != len(question.criteria):
        raise ValueError(f"expected {len(question.criteria)} answers, got {answers!r:.200}")
    grades = []
    for c, a in zip(question.criteria, answers):  # answers come back in the order asked
        if a.get("id") not in (None, c.id):
            raise ValueError(f"answer order mismatch: expected {c.id}, got {a.get('id')}")
        if c.kind == "binary":
            prob = float(a["probability"])
            met = prob >= threshold
            grades.append(CriterionGrade(criterion_id=c.id, points=c.points_for(met=met),
                                         label="met" if met else "not_met", probability=prob,
                                         confidence=abs(prob - 0.5) * 2))
        elif c.kind == "choice":
            lbl = a["choice"]
            grades.append(CriterionGrade(criterion_id=c.id, points=c.points_for(level=lbl), label=lbl))
        else:
            s = float(a["score"])
            grades.append(CriterionGrade(criterion_id=c.id, points=c.points_for(score=s), score=s))
    u = resp.get("usage") or {}
    usage = Usage(input_tokens=int(u.get("input_tokens", 0)), output_tokens=int(u.get("output_tokens", 0)), calls=1)
    return grades, usage.to_dict()


def default_transport() -> Transport:
    """POST via the openai client's generic `post`, so we do not depend on an SDK method name."""
    import openai

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")
    client = openai.AsyncOpenAI()  # reads OPENAI_API_KEY (and OPENAI_BASE_URL if set)

    async def post(path: str, body: dict) -> dict:
        return await client.post(path, body=body, cast_to=object)

    return post


class OpenAIDecisionsGrader:
    name = "openai"

    def __init__(self, transport: Transport | None = None, *, model: str = DEFAULT_DECISIONS_MODEL,
                 threshold: float = 0.5, retry_delay: float = 1.0, attempts: int = 4):
        self.transport = transport or default_transport()
        self.model, self.threshold = model, threshold
        self.retry_delay, self.attempts = retry_delay, attempts

    async def grade(self, rubric: Rubric, question: Question, exam_text: str, answer_text: str) -> GraderResult:
        rubric.require_locked()
        body = build_request(rubric, question, answer_text, self.model, exam_text)  # ONE request per answer per question

        async def once() -> GraderResult:
            resp = await self.transport("/decisions", body)
            grades, usage = parse_response(resp, question, threshold=self.threshold)
            return GraderResult(grader=self.name, model=self.model, served_model=resp.get("model"),
                                question_id=question.id, grades=grades, usage=usage)

        return await with_retries(once, attempts=self.attempts, base_delay=self.retry_delay)
