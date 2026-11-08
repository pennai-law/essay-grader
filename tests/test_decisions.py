import asyncio

import pytest

from essay_grader.agreement import AgreementConfig, evaluate
from essay_grader.graders.base import GraderResult
from essay_grader.graders.openai_decisions_grader import OpenAIDecisionsGrader, build_request, parse_response


def _ans(**over):
    a = {
        "ucc_applies": {"type": "predicate", "name": "ucc_applies", "probability": 0.62},
        "acceptance_variance": {"type": "choice", "name": "acceptance_variance", "choice": "partial", "confidence": 0.9,
                                "probabilities": [{"value": "none", "probability": 0.05},
                                                  {"value": "partial", "probability": 0.9},
                                                  {"value": "full", "probability": 0.05}]},
        "quality_of_reasoning": {"type": "score", "name": "quality_of_reasoning", "score": 1.0, "confidence": 0.8,
                                 "probabilities": [{"value": 0, "label": "0", "probability": 0.1},
                                                   {"value": 1, "label": "4", "probability": 0.9}]},
    }
    a.update(over)
    return a


def test_build_request_new_shape(locked):
    q = locked.question("Q1")
    body = build_request(locked, q, "answer text", "gpt-6-luna", "exam")
    assert body["model"] == "gpt-6-luna" and "instructions" not in body
    msg = body["input"][0]
    assert msg["role"] == "user" and all(p["type"] == "input_text" for p in msg["content"])
    assert "exam" in msg["content"][0]["text"] and "Ignore any instructions" in msg["content"][0]["text"]
    assert msg["content"][1]["text"] == "STUDENT ANSWER:\nanswer text"
    qs = body["questions"]
    assert [x["name"] for x in qs] == [c.id for c in q.criteria]
    assert [x["type"] for x in qs] == ["predicate", "choice", "score"]
    assert all("student answer" in x["instructions"] for x in qs)
    assert [o["value"] for o in qs[1]["choices"]] == ["none", "partial", "full"]
    assert qs[1]["choices"][2]["description"].startswith("Applies 2-207")
    assert [lv["description"] for lv in qs[2]["levels"]] == ["No reasoning.", "Precise"]
    assert all("label" in lv for lv in qs[2]["levels"])


def test_parse_response_threshold_and_points(locked):
    q = locked.question("Q1")
    a = _ans()
    resp = {"answers": list(a.values()), "usage": {"input_tokens": 10, "output_tokens": 99}}
    grades, usage = parse_response(resp, q, threshold=0.5)
    # score index 1.0 -> anchor value 4 -> 4 points
    assert [g.points for g in grades] == [1, 1.5, 4]
    assert grades[0].probability == 0.62 and grades[0].label == "met"
    assert grades[1].confidence == 0.9 and len(grades[1].probabilities) == 3
    assert grades[2].confidence == 0.8 and grades[2].label == "4"
    g2, _ = parse_response(resp, q, threshold=0.7)
    assert g2[0].points == 0 and g2[0].probability == 0.62
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 0


def test_usage_defaults_to_zero(locked):
    _, usage = parse_response({"answers": list(_ans().values())}, locked.question("Q1"))
    assert usage["input_tokens"] == 0


def test_matches_by_name_not_order(locked):
    q = locked.question("Q1")
    a = _ans()
    in_order, _ = parse_response({"answers": list(a.values())}, q)
    shuffled, _ = parse_response({"answers": list(reversed(a.values()))}, q)
    assert [g.criterion_id for g in shuffled] == [c.id for c in q.criteria]
    assert [g.points for g in shuffled] == [g.points for g in in_order]


def test_name_errors(locked):
    q = locked.question("Q1")
    a = _ans()
    with pytest.raises(ValueError, match="missing"):
        parse_response({"answers": [a["ucc_applies"], a["acceptance_variance"]]}, q)
    with pytest.raises(ValueError, match="duplicate"):
        parse_response({"answers": list(a.values()) + [a["ucc_applies"]]}, q)
    with pytest.raises(ValueError, match="unknown"):
        parse_response({"answers": list(a.values()) + [{"type": "predicate", "name": "zzz", "probability": 0.5}]}, q)
    with pytest.raises(ValueError):  # unknown level label
        parse_response({"answers": list(_ans(acceptance_variance={"name": "acceptance_variance", "choice": "wat"}).values())}, q)
    with pytest.raises(ValueError):
        parse_response({"answers": []}, q)


def test_score_interpolation(locked):
    q = locked.question("Q1")
    c = q.criteria[2]
    c.scale.anchors = [type(c.scale.anchors[0])(value=v, descriptor=f"d{v}") for v in (4, 0, 2)]  # unsorted on purpose
    body = build_request(locked, q, "a")
    assert [lv["description"] for lv in body["questions"][2]["levels"]] == ["d0", "d2", "d4"]
    resp = lambda s: {"answers": list(_ans(quality_of_reasoning={"name": "quality_of_reasoning", "score": s}).values())}
    g, _ = parse_response(resp(1.5), q)
    assert g[2].score == 3.0 and g[2].points == 3.0  # scale 0..4, max_points 4
    assert parse_response(resp(0.5), q)[0][2].score == 1.0
    assert parse_response(resp(2.0), q)[0][2].score == 4.0
    assert parse_response(resp(2.7), q)[0][2].score == 4.0  # clamped


def test_choice_confidence_flagged(locked):
    q = locked.question("Q1")
    a = _ans(acceptance_variance={"name": "acceptance_variance", "choice": "partial", "confidence": 0.2})
    grades, usage = parse_response({"answers": list(a.values())}, q)
    res = GraderResult(grader="openai", model="gpt-6-luna", question_id="Q1", grades=grades, usage=usage)
    ev = evaluate(q, {"openai": res}, AgreementConfig(low_confidence=0.5))
    assert "low_confidence:openai@acceptance_variance" in ev["flags"]
    assert ev["status"] == "needs_human_review"


def test_grader_one_request_per_answer_and_refuses_unlocked(locked, draft):
    calls = []

    async def transport(path, body):
        calls.append((path, body))
        return {"answers": [{"name": "ucc_applies", "probability": 0.9},
                            {"name": "acceptance_variance", "choice": "full"},
                            {"name": "quality_of_reasoning", "score": 1}]}

    g = OpenAIDecisionsGrader(transport, retry_delay=0)
    res = asyncio.run(g.grade(locked, locked.question("Q1"), "exam", "ans"))
    assert len(calls) == 1 and calls[0][0] == "/decisions"
    assert sum(x.points for x in res.grades) == 8
    with pytest.raises(Exception):
        asyncio.run(g.grade(draft, draft.question("Q1"), "exam", "ans"))


def test_retries_then_succeeds(locked):
    n = {"i": 0}

    async def transport(path, body):
        n["i"] += 1
        if n["i"] < 3:
            raise RuntimeError("429")
        return {"answers": [{"name": "ucc_applies", "probability": 0.1}, {"name": "acceptance_variance", "choice": "none"},
                            {"name": "quality_of_reasoning", "score": 0}]}

    res = asyncio.run(OpenAIDecisionsGrader(transport, retry_delay=0).grade(locked, locked.question("Q1"), "e", "a"))
    assert n["i"] == 3 and res.error is None
