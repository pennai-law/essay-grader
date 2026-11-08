"""Materials boundary (enforced in code) and the materials-naivety check."""
import os

import pytest

from essay_grader.study.design import Design
from essay_grader.study.materials import (ArmSandbox, MaterialsBoundaryError, MaterialsNaivetyError, check_materials,
                                          ensure_materials_naive, number_words, sandbox_for)
from test_study_helpers import design, design_path, make_study, store, protocol, run  # noqa: F401


# ---- boundary -------------------------------------------------------------------------------------------------
def test_arm_reads_only_its_own_folder(design):
    s = sandbox_for(design, "S")
    assert "Formation" in s.read_text("syllabus.md")
    names = [f.name for f in s.list_files()]
    assert names == ["syllabus.md"]  # S does NOT see outline.md, which lives in B's folder
    assert [f.name for f in sandbox_for(design, "B").list_files()] == ["outline.md", "syllabus.md"]


def test_reading_outside_the_arm_folder_raises(design, tmp_path):
    s = sandbox_for(design, "S")
    outline = design.path("materials/full/outline.md")
    for bad in ("../full/outline.md", outline, "/etc/hostname", design.path("exam/q1.txt"), "../../exam/answer_key.txt"):
        with pytest.raises(MaterialsBoundaryError):
            s.read_text(bad)


def test_sibling_folder_with_shared_prefix_is_not_inside(design):
    twin = design.path("materials/syllabus2")
    twin.mkdir()
    (twin / "secret.md").write_text("secret")
    with pytest.raises(MaterialsBoundaryError):
        sandbox_for(design, "S").read_text(twin / "secret.md")  # string-prefix matching would wrongly allow this


def test_symlink_escape_raises(design):
    root = design.path("materials/syllabus")
    os.symlink(design.path("exam/answer_key.txt"), root / "linked_key.txt")
    s = sandbox_for(design, "S")
    with pytest.raises(MaterialsBoundaryError):
        s.read_text("linked_key.txt")
    with pytest.raises(MaterialsBoundaryError):
        s.list_files()
    with pytest.raises(MaterialsBoundaryError):
        s.load()


def test_arm_a_has_no_materials_and_every_read_raises(design):
    s = sandbox_for(design, "A")
    assert s.load().files == [] and s.load().render() == ""
    with pytest.raises(MaterialsBoundaryError):
        s.read_text("anything.md")
    with pytest.raises(MaterialsBoundaryError):
        s.read_text(design.path("materials/full/outline.md"))


def test_manifest_has_hashes_and_render_wraps_only_this_arm(design):
    b = sandbox_for(design, "B").load()
    assert [f["path"] for f in b.manifest] == ["outline.md", "syllabus.md"]
    assert all(len(f["sha256"]) == 64 and f["bytes"] > 0 for f in b.manifest)
    assert b.render().startswith("<attached_materials>") and "Outline: Introductory Widget Law" in b.render()
    assert b.manifest_sha256 != sandbox_for(design, "S").load().manifest_sha256


def test_unsupported_material_type_is_reported_not_dropped(design):
    from essay_grader.study.materials import MaterialsError

    (design.path("materials/syllabus") / "slides.pptx").write_bytes(b"x")
    with pytest.raises(MaterialsError, match="unsupported"):
        sandbox_for(design, "S").load()


# ---- naivety ---------------------------------------------------------------------------------------------------
def _leak(tmp_path, text, name="leak.md", arm_dir="materials/full", mutate=None):
    p = make_study(tmp_path, mutate)
    (p.parent / arm_dir / name).write_text(text)
    return Design.load(p)


def test_fixture_materials_are_naive(design):
    rep = check_materials(design)
    assert rep.passed and not [f for f in rep.findings if f.severity == "block"]


def test_leaked_question_text_is_caught_with_evidence(tmp_path):
    d = _leak(tmp_path, "Study tip. In February a flood closed Glimmer's only factory. Glimmer wrote to Orbit that it would deliver "
                        "200 sprockets late and nothing more. Good luck.")
    rep = check_materials(d)
    hits = [f for f in rep.blocking if f.check == "ngram"]
    assert hits and hits[0].arm == "B" and hits[0].file == "leak.md" and "q1" in hits[0].detail
    assert "flood closed" in hits[0].evidence or "glimmer" in hits[0].evidence
    assert "BLOCKING" in rep.render() and "leak.md" in rep.render()
    with pytest.raises(MaterialsNaivetyError) as e:
        ensure_materials_naive(d)
    assert e.value.report.blocking


def test_leaked_mcq_text_is_caught_too(tmp_path):
    d = _leak(tmp_path, "Which remedy puts the injured buyer in the position performance would have? Expectation damages.", arm_dir="materials/syllabus")
    assert any(f.check == "ngram" and "mcq" in f.detail and f.arm == "S" for f in check_materials(d).blocking)


@pytest.mark.parametrize("text", ["Answers must not exceed 600 words.", "There is a 600-word limit on the essay.",
                                  "Word limit: 600", "The essay is capped at 600 words in total.", "a limit of 600"])
def test_exact_word_limit_is_caught(tmp_path, text):
    rep = check_materials(_leak(tmp_path, text))
    assert any(f.check == "word_limit" and f.severity == "block" for f in rep.findings), text


def test_word_limit_inside_a_range_is_a_warning_and_other_numbers_pass(tmp_path):
    rep = check_materials(_leak(tmp_path, "Essays run 400-600 words in past years. We covered 6000 words of reading."))
    assert [f.severity for f in rep.findings if f.check == "word_limit"] == ["warn"]
    assert check_materials(_leak(tmp_path / "b", "The 2026 casebook has 600 pages.")).passed


@pytest.mark.parametrize("text", ["The exam has 5 multiple choice questions.", "Five multiple-choice questions and one essay.",
                                  "Part One: MCQ (5)", "a 5-question multiple choice section"])
def test_exact_mcq_count_is_caught(tmp_path, text):
    rep = check_materials(_leak(tmp_path, text))
    assert any(f.check == "mcq_count" and f.severity == "block" for f in rep.findings), text


def test_generic_range_language_is_in_scope(tmp_path):
    # exam-taker 4.3.1: "20-30 multiple choice questions plus one or two essays" is allowed; an endpoint equal to N only warns
    rep = check_materials(_leak(tmp_path, "Expect 3-5 multiple choice questions plus one or two essays."))
    assert rep.passed and [f.severity for f in rep.findings if f.check == "mcq_count"] == ["warn"]
    assert check_materials(_leak(tmp_path / "b", "Expect 20 to 30 multiple choice questions.")).passed


@pytest.mark.parametrize("name", ["answer_key.txt", "Answer Key.md", "grading_rubric.md", "rubric.yaml", "model-answers.md",
                                  "final_exam_instructions.md", "key.txt", "scoring-guide.md"])
def test_rubric_and_answer_key_filenames_are_caught(tmp_path, name):
    rep = check_materials(_leak(tmp_path, "harmless content", name=name))
    assert any(f.check == "filename" and f.severity == "block" for f in rep.findings), name


def test_innocent_filenames_pass(tmp_path):
    for name in ("keynote_notes.md", "monkey_case.md", "outline_part2.md"):
        assert check_materials(_leak(tmp_path / name, "harmless", name=name)).passed, name


def test_copy_of_the_exam_file_is_caught(tmp_path):
    p = make_study(tmp_path)
    (p.parent / "materials/full/notes.md").write_bytes((p.parent / "exam/q1.txt").read_bytes())
    rep = check_materials(Design.load(p))
    assert {f.check for f in rep.blocking} >= {"exam_file_copy", "ngram"}


def test_guide_grep_is_a_nonblocking_warning(tmp_path):
    rep = check_materials(_leak(tmp_path, "Work each fact pattern carefully."))
    assert rep.passed and any(f.check == "guide_grep" and f.severity == "warn" for f in rep.findings)


def test_documented_exception_downgrades_to_allowed(tmp_path):
    def m(d):
        d["naivety"] = {"allow": [{"arm": "B", "file": "answer_key.txt", "check": "filename",
                                   "reason": "Practice-problem key from week 3, reviewed by Polk; unrelated to this exam."}]}
    d = _leak(tmp_path, "harmless", name="answer_key.txt", mutate=m)
    rep = check_materials(d)
    assert rep.passed and any(f.severity == "allowed" and "Practice-problem" in f.allow_reason for f in rep.findings)


def test_run_is_blocked_by_naivety_hits_and_makes_no_calls(tmp_path):
    d = _leak(tmp_path / "leaky", "Glimmer wrote to Orbit that it would deliver 200 sprockets late and nothing more.")
    from essay_grader.study.store import StudyStore

    st = StudyStore(tmp_path / "out2", d)
    with pytest.raises(MaterialsNaivetyError):
        run(d, st)
    assert not (st.root / "captures").exists() or not list((st.root / "captures").iterdir())


def test_number_words():
    assert number_words(5) == ["five"] and number_words(42) == ["forty-two", "forty two"] and number_words(100) == []
