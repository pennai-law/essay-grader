import json
import shutil

import pytest
import yaml

from essay_grader import questionnaire as qm
from essay_grader.cli import main
from essay_grader.questionnaire import Option, RawQuestion, Rewrite, apply_answers, build_questionnaire
from essay_grader.rubric import load_rubric


def rq(cid="ucc_applies", scenario="scenario text alpha", why="spread was large here", sev=3, aid="SYN001", opts=None,
       rec=None, excerpt=""):
    opts = opts or [Option(label="A", ruling="Rule A applies."), Option(label="B", ruling="Rule B applies.")]
    return RawQuestion(criterion_id=cid, scenario=scenario, excerpt=excerpt, answer_id=aid, why_it_matters=why,
                       severity=sev, options=opts, recommended=rec or opts[0].label)


def test_question_schema_validation():
    with pytest.raises(ValueError):  # 2-4 options
        qm.Question(id="H1", scenario="s", why_it_matters="w", options=[Option(label="A", ruling="x")], recommended="A")
    with pytest.raises(ValueError):  # recommended must be an option
        qm.Question(id="H1", scenario="s", why_it_matters="w", recommended="Z",
                    options=[Option(label="A", ruling="x"), Option(label="B", ruling="y")])
    with pytest.raises(ValueError):  # ruling XOR rewrite
        qm.Question(id="H1", scenario="s", why_it_matters="w", recommended="A",
                    options=[Option(label="A", ruling="x", rewrite=Rewrite(target="description", text="t")), Option(label="B", ruling="y")])
    q = qm.Question(id="H1", scenario="s", why_it_matters="w", recommended="A",
                    options=[Option(label="A", ruling="x"), Option(label="B", ruling="y")])
    assert q.allow_custom is True and q.criterion_id is None


def test_ranking_by_weight_times_spread_times_severity(draft):
    # ucc_applies max 1 pt, acceptance_variance max 3 pt
    raw = [rq("ucc_applies", "alpha topic words", "first why words", sev=3),
           rq("acceptance_variance", "bravo different subject matter", "second reasons entirely", sev=3),
           rq("ucc_applies", "charlie other subject again", "third distinct explanation", sev=5)]
    qn = build_questionnaire(draft, raw, run_id="r", crit_spread={"ucc_applies": 0.5, "acceptance_variance": 0.5})
    total = sum(c.max_points for c in draft.questions[0].criteria)
    w1, w3 = 1 / total, 3 / total
    by = {q.scenario: q.impact for q in qn.questions}
    assert by["alpha topic words"] == pytest.approx(w1 * 0.5 * 3, rel=1e-3)
    assert by["bravo different subject matter"] == pytest.approx(w3 * 0.5 * 3, rel=1e-3)
    assert by["charlie other subject again"] == pytest.approx(w1 * 0.5 * 5, rel=1e-3)
    assert [q.scenario for q in qn.questions][0] == "bravo different subject matter"  # biggest impact first
    assert [q.id for q in qn.questions] == ["H1", "H2", "H3"]


def test_outcome_gap_and_policy_questions_rank(draft):
    policy = rq(None, "rabbit hole answer scored high", "pedagogically wrong outcome", sev=5, aid="SYN004",
                opts=[Option(label="strict", ruling="Off-topic effort earns nothing."),
                      Option(label="lenient", rewrite=Rewrite(target="policy:off_topic_policy", text="Ignore it."))])
    minor = rq("ucc_applies", "tiny wording thing", "negligible", sev=1)
    qn = build_questionnaire(draft, [minor, policy], run_id="r", answer_gap={"SYN004": 0.5})
    assert qn.questions[0].criterion_id is None and qn.questions[0].impact == pytest.approx(1.0 * 0.5 * 5)


def test_dedupe_near_duplicates_keeps_higher_impact(draft):
    a = rq("ucc_applies", "The strong answer cites section 2-102 but the rubric wording is vague", "Spread of two points across repeats", sev=2)
    b = rq("ucc_applies", "The strong answer cites section 2-102 though the rubric wording is vague", "Spread of two points across repeats", sev=4)
    c = rq("acceptance_variance", "The strong answer cites section 2-102 but the rubric wording is vague", "Spread of two points across repeats", sev=4)
    qn = build_questionnaire(draft, [a, b, c], run_id="r")
    assert len(qn.questions) == 2  # a/b merged (same criterion); c kept (different criterion)
    kept = next(q for q in qn.questions if q.criterion_id == "ucc_applies")
    assert kept.severity == 4 and any("near-duplicate" in w for w in qn.warnings)


def test_max_questions_cap(draft):
    raw = [rq("ucc_applies", f"unique{i}a unique{i}b unique{i}c unique{i}d", f"why{i}a why{i}b why{i}c why{i}d", sev=1 + i % 5)
           for i in range(20)]
    qn = build_questionnaire(draft, raw, run_id="r", max_questions=12)
    assert len(qn.questions) == 12 and any("omitted" in w for w in qn.warnings)
    assert build_questionnaire(draft, raw, run_id="r", max_questions=3).questions[0].severity == 5
    assert len(build_questionnaire(draft, raw, run_id="r").questions) == 12  # default cap is 12


def test_invalid_questions_and_options_are_dropped(draft):
    bad_crit = rq("nope")
    bad_target = rq("ucc_applies", opts=[Option(label="A", rewrite=Rewrite(target="level:zzz", text="x")),
                                         Option(label="B", ruling="ok"), Option(label="C", ruling="fine")], rec="A")
    only_bad = rq("ucc_applies", "other words here", "zzz yyy xxx", opts=[Option(label="A", rewrite=Rewrite(target="level:zzz", text="x")),
                                                                         Option(label="B", ruling="ok")])
    qn = build_questionnaire(draft, [bad_crit, bad_target, only_bad], run_id="r")
    assert len(qn.questions) == 1 and [o.label for o in qn.questions[0].options] == ["B", "C"]
    assert qn.questions[0].recommended == "B"  # recommended defaulted because "A" was dropped


def test_excerpt_must_be_verbatim_from_the_cited_answer(draft):
    texts = {"SYN001": "The offer was accepted by performance and consideration exists."}
    good = rq(excerpt="accepted by performance", aid="SYN001")
    bad = rq("acceptance_variance", "other stuff words here", "unrelated reasons here", excerpt="invented quote", aid="SYN001")
    qn = build_questionnaire(draft, [good, bad], run_id="r", answer_texts=texts)
    ex = {q.criterion_id: q.excerpt for q in qn.questions}
    assert ex["ucc_applies"] == "accepted by performance"
    assert ex["acceptance_variance"] == texts["SYN001"]  # fell back to a real slice of the answer


def _qn(draft):
    raw = [rq("ucc_applies", "alpha topic words", "first why words", sev=5,
              opts=[Option(label="Strict", ruling="Must cite the section."), Option(label="Loose", ruling="Citation not needed.")]),
           rq("acceptance_variance", "bravo other subject", "second reasons here", sev=4,
              opts=[Option(label="Rewrite level", rewrite=Rewrite(target="level:full", text="NEW full descriptor")),
                    Option(label="Rewrite desc", rewrite=Rewrite(target="description", text="NEW description")),
                    Option(label="Keep", )], rec="Rewrite level"),
           rq(None, "policy scenario charlie", "third explanation again", sev=3,
              opts=[Option(label="No credit", ruling="Rabbit holes earn nothing."),
                    Option(label="Rewrite policy", rewrite=Rewrite(target="policy:off_topic_policy", text="Penalize tangents."))])]
    return build_questionnaire(draft, raw, run_id="hrun-1", crit_spread={"ucc_applies": 0.5, "acceptance_variance": 0.5}, answer_gap={"SYN001": 0.2})


def test_apply_answers_provenance_defaults_and_versioning(draft):
    qn = _qn(draft)
    ids = {q.criterion_id: q.id for q in qn.questions}
    out = apply_answers(draft, qn, {ids["ucc_applies"]: "loose", ids["acceptance_variance"]: "Rewrite desc"})
    assert out.version == draft.version + 1 and not out.locked and out.content_hash is None
    ucc = next(c for c in out.questions[0].criteria if c.id == "ucc_applies")
    assert ucc.edge_case_rulings == ["Citation not needed."]  # label match is case-insensitive
    acc = next(c for c in out.questions[0].criteria if c.id == "acceptance_variance")
    assert acc.description == "NEW description"
    assert out.policies.additional_rulings == ["Rabbit holes earn nothing."]  # unanswered -> recommended
    by_q = {p.question_id: p for p in out.provenance}
    assert set(by_q) == {q.id for q in qn.questions}
    assert all(p.hardening_run_id == "hrun-1" for p in out.provenance)
    assert by_q[ids["ucc_applies"]].source == "answer" and by_q[ids["ucc_applies"]].option_label == "Loose"
    assert by_q[ids[None]].source == "recommended_default" and by_q[ids[None]].option_label == "No credit"
    assert by_q[ids["acceptance_variance"]].action == "rewrite_descriptor" and by_q[ids["acceptance_variance"]].before
    assert draft.version == 1 and not draft.policies.additional_rulings  # input untouched
    locked = out.lock()
    locked.require_locked()
    assert "RULING (rubric-wide): Rabbit holes earn nothing." in __import__("essay_grader.prompts", fromlist=["x"]).render_policies(locked)


def test_apply_answers_all_defaults_custom_and_errors(draft):
    qn = _qn(draft)
    out = apply_answers(draft, qn, {})
    assert {p.source for p in out.provenance} == {"recommended_default"}
    assert len(out.provenance) == len(qn.questions)
    q_ucc = next(q for q in qn.questions if q.criterion_id == "ucc_applies")
    out = apply_answers(draft, qn, {q_ucc.id: "Grade only the conclusion, never the citation."})
    p = next(p for p in out.provenance if p.question_id == q_ucc.id)
    assert p.source == "custom" and p.text == "Grade only the conclusion, never the citation."
    assert "Grade only the conclusion, never the citation." in next(c for c in out.questions[0].criteria if c.id == "ucc_applies").edge_case_rulings
    with pytest.raises(ValueError, match="unknown question ids"):
        apply_answers(draft, qn, {"H99": "x"})
    stale = draft.model_copy(deep=True)
    stale.questions[0].criteria[0].description += " edited"
    with pytest.raises(ValueError, match="different rubric"):
        apply_answers(stale, qn, {})
    assert apply_answers(stale, qn, {}, allow_stale=True)


def test_old_locked_hashes_stay_valid(locked):
    # fields added for provenance/rulings are omitted from the hash while empty
    import hashlib

    data = locked.model_dump(mode="json", exclude={"locked", "content_hash", "locked_at", "provenance"})
    data["policies"].pop("additional_rulings")
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert locked.content_hash == hashlib.sha256(blob.encode()).hexdigest()  # the pre-existing hash recipe


def test_markdown_lists_questions_and_template(draft):
    md = qm.render_markdown(_qn(draft))
    assert "answers.yaml template" in md and "(recommended)" in md and "H1:" in md and "own ruling" in md


def test_cli_harden_apply_answers_lock_grade_flow(fix, tmp_path):
    shutil.copy(fix / "rubric.yaml", tmp_path / "draft.yaml")
    out = tmp_path / "h"
    assert main(["harden", "--exam", str(fix / "exam.txt"), "--syllabus", str(fix / "exam.txt"), "--rubric", str(tmp_path / "draft.yaml"),
                 "--dry-run", "--budget-usd", "5", "--out", str(out)]) == 0
    d = next(out.glob("harden-*"))
    qj = d / "questionnaire.json"
    assert qj.exists() and (d / "questionnaire.md").exists()
    qn = json.loads(qj.read_text())
    assert 0 < len(qn["questions"]) <= 12
    answers = tmp_path / "answers.yaml"
    answers.write_text(yaml.safe_dump({qn["questions"][0]["id"]: qn["questions"][0]["options"][-1]["label"]}))
    assert main(["apply-answers", "--rubric", str(tmp_path / "draft.yaml"), "--questionnaire", str(qj), "--answers", str(answers)]) == 0
    v2 = tmp_path / "draft.v2.yaml"
    assert load_rubric(v2).version == 2 and load_rubric(v2).provenance
    assert main(["lock", str(v2)]) == 0
    assert main(["grade", "--rubric", str(tmp_path / "draft.v2.locked.yaml"), "--exam", str(fix / "exam.txt"), "--answers",
                 str(fix / "answers"), "--dry-run", "--out", str(tmp_path / "runs"), "--run-id", "x"]) == 0
    # a bad answers file is an error, not a crash
    answers.write_text(yaml.safe_dump({"H99": "x"}))
    assert main(["apply-answers", "--rubric", str(tmp_path / "draft.yaml"), "--questionnaire", str(qj), "--answers", str(answers)]) == 2
    # budget too small for even the minimal plan -> exit 4
    assert main(["harden", "--exam", str(fix / "exam.txt"), "--syllabus", str(fix / "exam.txt"), "--rubric", str(tmp_path / "draft.yaml"),
                 "--dry-run", "--budget-usd", "0.001", "--out", str(out)]) == 4


# ---- per-option projected effects (consequences shown to the professor) ---------------------------------------------

def _pe(cid, before, after):
    return qm.ProjectedEffect(criterion_id=cid, before_points=before, after_points=after)


def test_old_questionnaire_without_projected_still_loads():
    old = {"schema_version": 1, "run_id": "r", "run_kind": "hardening", "exam_id": "e", "rubric_version": 1,
           "draft_rubric_hash": "x", "questions": [{"id": "H1", "scenario": "s", "why_it_matters": "w", "recommended": "A",
                                                    "options": [{"label": "A", "ruling": "x"}, {"label": "B", "ruling": "y"}]}]}
    qn = qm.Questionnaire.model_validate(old)
    assert all(o.projected is None for o in qn.questions[0].options)
    assert "Projected by AI" not in qm.render_markdown(qn)


def test_projected_is_clamped_to_criterion_range_and_before_anchored(draft):
    # ucc_applies has max 1 point, acceptance_variance max 3
    opts = [Option(label="A", ruling="Rule A applies.", projected=[_pe("acceptance_variance", 9.0, -2.0)]),
            Option(label="B", ruling="Rule B applies.", projected=[_pe("acceptance_variance", 9.0, 7.5)])]
    qn = build_questionnaire(draft, [rq("acceptance_variance", opts=opts)], run_id="r",
                             observed_points={"SYN001": {"acceptance_variance": 2.0}})
    a, b = qn.questions[0].options
    assert (a.projected[0].before_points, a.projected[0].after_points) == (2.0, 0.0)   # before = measured, after clamped up
    assert (b.projected[0].before_points, b.projected[0].after_points) == (2.0, 3.0)   # clamped down to max_points
    # and without observed data the model's before is only clamped
    qn2 = build_questionnaire(draft, [rq("acceptance_variance", opts=opts)], run_id="r")
    assert qn2.questions[0].options[0].projected[0].before_points == 3.0


def test_projected_drops_unknown_criteria_duplicates_and_nan(draft):
    nan = float("nan")
    opts = [Option(label="A", ruling="Rule A applies.", projected=[_pe("nope", 1, 0), _pe("ucc_applies", nan, 0), _pe("ucc_applies", 1, 0),
                                                                   _pe("ucc_applies", 1, 1)]),
            Option(label="B", ruling="Rule B applies.", projected=[_pe("nope", 1, 0)])]
    qn = build_questionnaire(draft, [rq("ucc_applies", opts=opts)], run_id="r")
    a, b = qn.questions[0].options
    assert [(p.criterion_id, p.before_points, p.after_points) for p in a.projected] == [("ucc_applies", 1.0, 0.0)]
    assert b.projected is None
    assert any("unknown criterion 'nope'" in w for w in qn.warnings) and any("non-numeric" in w for w in qn.warnings)


def test_calibration_questionnaires_carry_no_projections(draft):
    opts = [Option(label="A", ruling="Rule A applies.", projected=[_pe("ucc_applies", 1, 0)]), Option(label="B", ruling="Rule B applies.")]
    qn = build_questionnaire(draft, [rq("ucc_applies", opts=opts)], run_id="c", run_kind="calibration", id_prefix="C")
    assert all(o.projected is None for o in qn.questions[0].options)


def test_render_markdown_shows_projection(draft):
    opts = [Option(label="A", ruling="Rule A applies.", projected=[_pe("acceptance_variance", 2, 1)]), Option(label="B", ruling="Rule B applies.")]
    md = qm.render_markdown(build_questionnaire(draft, [rq("acceptance_variance", opts=opts)], run_id="r"))
    assert "Projected by AI on the sample answer: acceptance_variance 2 -> 1." in md
