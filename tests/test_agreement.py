import math

from essay_grader.agreement import AgreementConfig, cohen_kappa, evaluate
from essay_grader.graders.base import CriterionGrade, GraderResult


def test_kappa_values():
    assert cohen_kappa(list("aabb"), list("aabb")) == 1.0
    assert cohen_kappa([1, 1], [1, 1]) is None  # undefined when chance agreement is 1
    assert math.isclose(cohen_kappa([1, 1, 0, 0], [1, 0, 1, 0]), 0.0, abs_tol=1e-9)
    assert math.isclose(cohen_kappa([1, 0, 1, 0], [0, 1, 0, 1]), -1.0)


def test_kappa_exact():
    a = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    b = ["y"] * 20 + ["y"] * 5 + ["n"] * 10 + ["n"] * 15
    # po = 35/50 = .7 ; pa(y)=.6/pb(y)=.5 -> pe = .3+.2 = .5 ; k = .4
    assert math.isclose(cohen_kappa(a, b), 0.4)


def _res(name, q, vals, conf=0.9, bad=()):
    gs = []
    for c, v in zip(q.criteria, vals):
        if c.kind == "binary":
            gs.append(CriterionGrade(criterion_id=c.id, points=c.points_for(met=v), label="met" if v else "not_met", confidence=conf))
        elif c.kind == "choice":
            gs.append(CriterionGrade(criterion_id=c.id, points=c.points_for(level=v), label=v, confidence=conf))
        else:
            gs.append(CriterionGrade(criterion_id=c.id, points=c.points_for(score=v), score=v, confidence=conf,
                                     invalid_quotes=list(bad)))
    return GraderResult(grader=name, model="m", question_id=q.id, grades=gs)


def test_agree_and_combine(draft):
    q = draft.question("Q1")
    ev = evaluate(q, {"claude": _res("claude", q, [True, "full", 3.0]), "openai": _res("openai", q, [True, "full", 4.0])}, AgreementConfig())
    assert ev["status"] == "agreed"  # score diff of 1 point is not > 1
    assert ev["suggested_total"] == 1 + 3 + 3.5


def test_disagreements(draft):
    q = draft.question("Q1")
    cfg = AgreementConfig()
    ev = evaluate(q, {"claude": _res("claude", q, [True, "full", 1.0]), "openai": _res("openai", q, [True, "full", 3.0])}, cfg)
    assert ev["status"] == "needs_human_review" and "disagree@quality_of_reasoning" in ev["flags"]
    assert ev["suggested_total"] is None
    ev = evaluate(q, {"claude": _res("claude", q, [True, "full", 2.0]), "openai": _res("openai", q, [True, "partial", 2.0])}, cfg)
    assert "disagree@acceptance_variance" in ev["flags"]  # choice level mismatch even though diff is 1.5? (>1)
    ev = evaluate(q, {"claude": _res("claude", q, [True, "full", 2.0]), "openai": _res("openai", q, [False, "full", 2.0])}, cfg)
    assert "disagree@ucc_applies" in ev["flags"]  # binary mismatch flags despite 1pt diff


def test_quote_and_confidence_and_error_flags(draft):
    q = draft.question("Q1")
    ev = evaluate(q, {"claude": _res("claude", q, [True, "full", 2.0], bad=["made up"]), "openai": _res("openai", q, [True, "full", 2.0])}, AgreementConfig())
    assert ev["status"] == "needs_human_review" and any(f.startswith("invalid_quotes") for f in ev["flags"])
    ev = evaluate(q, {"claude": _res("claude", q, [True, "full", 2.0], conf=0.2), "openai": _res("openai", q, [True, "full", 2.0])}, AgreementConfig())
    assert any(f.startswith("low_confidence") for f in ev["flags"])
    err = GraderResult(grader="openai", model="m", question_id="Q1", error="boom")
    ev = evaluate(q, {"claude": _res("claude", q, [True, "full", 2.0]), "openai": err}, AgreementConfig())
    assert ev["status"] == "needs_human_review" and "grader_error:openai" in ev["flags"]


def test_single_grader(draft):
    q = draft.question("Q1")
    ev = evaluate(q, {"claude": _res("claude", q, [True, "full", 2.0])}, AgreementConfig())
    assert ev["status"] == "single_grader"
