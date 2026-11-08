import pytest
from pydantic import ValidationError

from essay_grader.rubric import Criterion, Rubric, RubricNotLocked, load_rubric, save_rubric


def test_load_and_points(draft):
    q = draft.question("Q1")
    assert q.max_points == 8
    c = {c.id: c for c in q.criteria}
    assert c["ucc_applies"].points_for(met=True) == 1
    assert c["acceptance_variance"].points_for(level="partial") == 1.5
    assert c["quality_of_reasoning"].points_for(score=2) == 2
    with pytest.raises(ValueError):
        c["acceptance_variance"].points_for(level="nope")
    with pytest.raises(ValueError):
        c["quality_of_reasoning"].points_for(score=9)


@pytest.mark.parametrize("bad", [
    dict(id="x", description="d", max_points=1, kind="choice", levels=[{"label": "a", "points": 0, "descriptor": "d"}]),
    dict(id="x", description="d", max_points=1, kind="score"),
    dict(id="x", description="d", max_points=1, kind="choice",
         levels=[{"label": "a", "points": 0, "descriptor": "d"}, {"label": "b", "points": 5, "descriptor": "d"}]),
    dict(id="x", description="d", max_points=0, kind="binary"),
])
def test_invalid_criteria(bad):
    with pytest.raises(ValidationError):
        Criterion(**bad)


def test_length_policy_requires_cap():
    with pytest.raises(ValidationError):
        Rubric.model_validate({"exam_id": "e", "policies": {"length_policy": {"mode": "deduct"}},
                               "questions": [{"id": "q", "criteria": [dict(id="a", description="d", max_points=1, kind="binary")]}]})


def test_lock_hash_and_tamper(draft, tmp_path):
    with pytest.raises(RubricNotLocked):
        draft.require_locked()
    lk = draft.lock()
    lk.require_locked()
    assert len(lk.content_hash) == 64
    assert lk.content_hash == draft.lock().content_hash  # deterministic, independent of locked_at
    p = save_rubric(lk, tmp_path / "r.locked.yaml")
    again = load_rubric(p)
    again.require_locked()
    again.questions[0].criteria[0].max_points = 2
    with pytest.raises(RubricNotLocked):
        again.require_locked()


def test_rulings_change_hash_and_bump_version(locked):
    new = locked.with_rulings([("ucc_applies", "Credit 'sale of goods' wording.")])
    assert new.version == 2 and not new.locked
    assert new.lock().content_hash != locked.content_hash
