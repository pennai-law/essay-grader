import asyncio

import pytest

from essay_grader.graders.openai_decisions_grader import OpenAIDecisionsGrader, build_request, parse_response


def test_build_request_maps_kinds_in_order(locked):
    q = locked.question("Q1")
    body = build_request(locked, q, "answer text", "gpt-6-luna", "exam")
    assert body["model"] == "gpt-6-luna" and body["input"] == "answer text"
    qs = body["questions"]
    assert [x["id"] for x in qs] == [c.id for c in q.criteria]  # one question per criterion, same order
    assert [x["type"] for x in qs] == ["predicate", "choice", "score"]
    assert [o["value"] for o in qs[1]["options"]] == ["none", "partial", "full"]
    assert qs[1]["options"][2]["description"].startswith("Applies 2-207")
    assert (qs[2]["min"], qs[2]["max"]) == (0, 4) and len(qs[2]["anchors"]) == 2


def test_parse_response_threshold_and_points(locked):
    q = locked.question("Q1")
    resp = {"answers": [{"id": "ucc_applies", "probability": 0.62}, {"id": "acceptance_variance", "choice": "partial"},
                        {"id": "quality_of_reasoning", "score": 3}], "usage": {"input_tokens": 10, "output_tokens": 2}}
    grades, usage = parse_response(resp, q, threshold=0.5)
    assert [g.points for g in grades] == [1, 1.5, 3]
    assert grades[0].probability == 0.62 and grades[0].label == "met"
    g2, _ = parse_response(resp, q, threshold=0.7)
    assert g2[0].points == 0 and g2[0].probability == 0.62
    assert usage["input_tokens"] == 10


def test_parse_response_errors(locked):
    q = locked.question("Q1")
    with pytest.raises(ValueError):
        parse_response({"answers": []}, q)
    with pytest.raises(ValueError):  # unknown level label
        parse_response({"answers": [{"probability": 1}, {"choice": "wat"}, {"score": 1}]}, q)


def test_grader_one_request_per_answer_and_refuses_unlocked(locked, draft):
    calls = []

    async def transport(path, body):
        calls.append((path, body))
        return {"answers": [{"probability": 0.9}, {"choice": "full"}, {"score": 4}]}

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
        return {"answers": [{"probability": 0.1}, {"choice": "none"}, {"score": 0}]}

    res = asyncio.run(OpenAIDecisionsGrader(transport, retry_delay=0).grade(locked, locked.question("Q1"), "e", "a"))
    assert n["i"] == 3 and res.error is None
