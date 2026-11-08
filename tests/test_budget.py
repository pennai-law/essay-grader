import asyncio

import pytest

from essay_grader.budget import Budget, BudgetExceeded
from essay_grader.fake import FakeLLM
from essay_grader.graders.claude_grader import GradeOutput
from essay_grader.llm import Usage


def test_estimator_is_chars_over_4_plus_max_tokens():
    # 4000 chars = 1000 input tokens; 500 output tokens worst case
    est = Budget.estimate("claude-opus-5-5", 4000, 500)
    assert est == pytest.approx((1000 * 4.0 + 500 * 20.0) / 1e6)
    assert Budget.estimate("claude-sonnet-5-5", 4000, 500) == pytest.approx((1000 * 2.0 + 500 * 10.0) / 1e6)
    assert Budget.estimate("unpriced-model", 4000, 500) == 0.0


def test_record_ledger_and_remaining():
    b = Budget(max_usd=1.0)
    cost = b.record("critique", "claude-opus-5-5", Usage(input_tokens=100_000, output_tokens=10_000, calls=1))
    assert cost == pytest.approx(0.4 + 0.2)
    assert b.spent_usd == pytest.approx(0.6) and b.remaining() == pytest.approx(0.4)
    e = b.ledger[0]
    assert (e.purpose, e.model, e.input_tokens, e.output_tokens) == ("critique", "claude-opus-5-5", 100_000, 10_000)
    assert b.summary()["by_purpose"][0]["cost_usd"] == pytest.approx(0.6)


def test_reserve_refuses_to_exceed_and_reservations_count():
    b = Budget(max_usd=0.05)
    est = Budget.estimate("claude-opus-5-5", 4000, 1000)  # $0.024
    r1 = b.reserve("claude-opus-5-5", 4000, 1000, "a")
    r2 = b.reserve("claude-opus-5-5", 4000, 1000, "b")
    assert b.remaining() == pytest.approx(0.05 - 2 * est)
    with pytest.raises(BudgetExceeded) as ei:
        b.reserve("claude-opus-5-5", 4000, 1000, "c")  # in-flight reservations count
    assert ei.value.estimate_usd == pytest.approx(est)
    b.release(r1 + r2)
    assert b.remaining() == pytest.approx(0.05)


def test_llm_call_is_skipped_when_over_budget_and_records_actuals_otherwise():
    llm = FakeLLM("claude-opus-5-5")
    tiny = Budget(max_usd=0.0001)
    with pytest.raises(BudgetExceeded):
        asyncio.run(llm.call(prefix="x" * 4000, user="u", schema=GradeOutput, max_tokens=4096, purpose="t", budget=tiny,
                             hint={"kind": "grade", "question": {"criteria": []}, "answer": "a"}))
    assert llm.calls == 0 and tiny.spent_usd == 0 and tiny.reserved_usd == 0  # no API call, nothing reserved
    ok = Budget(max_usd=5.0)
    asyncio.run(llm.call(prefix="p", user="u", schema=GradeOutput, max_tokens=4096, purpose="grade", budget=ok,
                         hint={"kind": "grade", "question": {"criteria": []}, "answer": "a"}))
    assert llm.calls == 1 and len(ok.ledger) == 1 and ok.ledger[0].purpose == "grade" and ok.spent_usd > 0
    assert ok.reserved_usd == 0  # released after the call
