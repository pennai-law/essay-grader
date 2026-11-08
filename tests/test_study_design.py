"""Design spec: loading, static validation, ExamID policy, arm folder disjointness."""
import pytest

from essay_grader.study.design import Cell, Design, DesignError, validate_design
from test_study_helpers import design, design_path, make_study, multi_model  # noqa: F401


def _validate(tmp_path, mutate=None):
    return validate_design(Design.load(make_study(tmp_path, mutate)))


def test_fixture_validates_clean(design):
    rep = validate_design(design)
    assert rep.ok and not rep.warnings, rep.render()
    assert [c.id for c in design.cells()][:3] == ["q1__A__sonnet", "q1__S__sonnet", "q1__B__sonnet"]
    assert design.examid_for("S", "sonnet") == "0460"
    assert Cell.parse("q1__S__sonnet") == Cell("q1", "S", "sonnet")


def test_unknown_keys_are_errors(tmp_path):
    p = make_study(tmp_path, lambda d: d.update(arms_typo=1))
    with pytest.raises(DesignError):
        Design.load(p)


def test_arm_a_cannot_have_materials_and_others_need_them(tmp_path):
    with pytest.raises(DesignError):
        Design.load(make_study(tmp_path, lambda d: d["arms"][0].update(materials="materials/full")))
    with pytest.raises(DesignError):
        Design.load(make_study(tmp_path / "b", lambda d: d["arms"][1].update(materials=None)))


def test_overlapping_arm_folders_are_an_error(tmp_path):
    def m(d):
        d["arms"][1]["materials"] = "materials"  # S would contain B's folder
    rep = _validate(tmp_path, m)
    assert any("overlap" in e for e in rep.errors)


def test_answer_key_inside_materials_folder_is_an_error(tmp_path):
    def m(d):
        d["components"][1]["answer_key_file"] = "materials/full/key.txt"
    p = make_study(tmp_path, m)
    (p.parent / "materials/full/key.txt").write_text("Q1: A\n")
    rep = validate_design(Design.load(p))
    assert any("inside its materials folder" in e for e in rep.errors)


def test_duplicate_examids_and_missing_ids(tmp_path):
    rep = _validate(tmp_path, lambda d: d["examids"].update(S="0137"))
    assert any("ExamID 0137 is used by both" in e for e in rep.errors)
    rep = _validate(tmp_path / "b", lambda d: d["examids"].pop("B"))
    assert any("no ExamID for arm B" in e for e in rep.errors)
    rep = _validate(tmp_path / "c", lambda d: d["examids"].update(A="12x4"))
    assert any("digits only" in e for e in rep.errors)


def test_examid_range_tell_and_unconfirmed_policy_warn(tmp_path):
    def m(d):
        d["examids"] = {"A": "1204", "B": "1225", "S": "1255"}  # the Wave 1 pattern: above the real range, clustered
        d["examid_policy"]["confirmed"] = False
    rep = _validate(tmp_path, m)
    text = " ".join(rep.warnings)
    assert "outside the real-student range" in text and "confirmed is false" in text


def test_clustered_ids_inside_range_warn(tmp_path):
    rep = _validate(tmp_path, lambda d: d["examids"].update(A="0500", S="0510", B="0520"))
    assert any("clustered" in w for w in rep.warnings)


def test_multi_model_needs_arm_model_examids(tmp_path):
    rep = _validate(tmp_path, lambda d: d["models"].append({"key": "gpt", "provider": "openai", "model": "gpt-6-luna"}))
    assert any("no ExamID for arm A / model" in e for e in rep.errors)
    rep = _validate(tmp_path / "b", multi_model)
    assert rep.ok, rep.render()
    d = Design.load(make_study(tmp_path / "c", multi_model))
    assert len(d.cells()) == 2 * 3 * 2 and d.examid_for("B", "gpt") == "0121"


def test_unknown_provider_and_unpriced_model_warning(tmp_path):
    rep = _validate(tmp_path, lambda d: d["models"][0].update(provider="mystery"))
    assert any("unknown provider" in e for e in rep.errors)
    rep = _validate(tmp_path / "b", lambda d: d["models"][0].update(model="brand-new-model-1"))
    assert any("no known price" in w for w in rep.warnings)


def test_missing_protocol_and_empty_materials_folder(tmp_path):
    rep = _validate(tmp_path, lambda d: d.update(protocol="nope"))
    assert any(e.startswith("protocol:") for e in rep.errors)
    p = make_study(tmp_path / "b")
    for f in (p.parent / "materials/syllabus").iterdir():
        f.unlink()
    assert any("empty" in e for e in validate_design(Design.load(p)).errors)
