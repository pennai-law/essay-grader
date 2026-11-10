"""A long points checklist (dozens of independently earnable binary criteria in one question) gets enough output room."""
import asyncio

from essay_grader.fake import FakeLLM
from essay_grader.graders.claude_grader import (GRADE_MAX_TOKENS, GRADE_MAX_TOKENS_CEILING, ClaudeGrader, expected_grade_tokens,
                                                grade_max_tokens)
from essay_grader.rubric import Rubric


def _rubric(n: int) -> Rubric:
    return Rubric.model_validate({"exam_id": "checklist", "questions": [{"id": "Q1", "criteria": [
        {"id": f"p{i}", "description": f"Makes point {i}", "max_points": 2, "kind": "binary"} for i in range(n)]}]}).lock()


def test_ordinary_rubrics_keep_the_old_limit():
    q = _rubric(16).questions[0]
    assert grade_max_tokens(q) == GRADE_MAX_TOKENS and expected_grade_tokens(q) == 1500


def test_a_fifty_point_checklist_gets_more_room_and_is_capped():
    q = _rubric(50).questions[0]
    assert grade_max_tokens(q) == GRADE_MAX_TOKENS + 256 * 34
    assert 1500 < expected_grade_tokens(q) <= grade_max_tokens(q)
    assert grade_max_tokens(_rubric(400).questions[0]) == GRADE_MAX_TOKENS_CEILING


def test_the_request_carries_the_scaled_limit_and_the_estimate_grows():
    r = _rubric(50)
    llm = FakeLLM()
    g = ClaudeGrader(llm, retry_delay=0)
    res = asyncio.run(g.grade(r, r.questions[0], "Exam text", "The defendant owed a duty of care and breached it."))
    assert res.error is None and len(res.grades) == 50
    assert llm.requests[0]["max_tokens"] == grade_max_tokens(r.questions[0])
    small = _rubric(10)
    assert g.estimate_cost(r, r.questions[0], "x", "y", worst_case=True) > g.estimate_cost(small, small.questions[0], "x", "y", worst_case=True)
