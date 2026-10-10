"""Revision pass: verbatim capture is never modified; mechanical fixes are deterministic; LLM pass is constrained and logged."""
import asyncio
import hashlib
import json
import os

import pytest

from essay_grader.study.audit import audit_text
from essay_grader.study.design import Cell
from essay_grader.study.providers import FakeProvider
from essay_grader.study.revise import (extract_mcq_block, fix_banned_phrases, fix_em_dashes, new_authority_tokens, revise_cell,
                                       strip_leading_preamble)
from test_study_helpers import CLEAN_ESSAY, CLEAN_MCQ, clean_script, design, design_path, protocol, run, store  # noqa: F401

DIRTY = ("Course materials are attached. Orbit's best claim is cover damages—Glimmer delivered only 200 of 500 sprockets. "
         "Notably, the 1990–2000 cases agree and 1990—2000 is a typo. Furthermore, Glimmer's defense is robust. "
         "This raises important questions about allocation.")


def _setup(design, store, script):
    run(design, store, providers={"anthropic": FakeProvider("anthropic", script=script)})


def _revise(design, store, protocol, cell, **kw):
    kw.setdefault("citations", False)
    return asyncio.run(revise_cell(design, store, cell, protocol, **kw))


def _fingerprint(path):
    st = os.stat(path)
    return hashlib.sha256(path.read_bytes()).hexdigest(), st.st_mtime_ns, st.st_mode


def test_revise_never_modifies_the_verbatim_capture(design, store, protocol):
    _setup(design, store, lambda r: DIRTY if r.cell_id == "q1__B__sonnet" else clean_script(r))
    cell = Cell("q1", "B", "sonnet")
    before = _fingerprint(store.capture_path(cell))
    meta_before = store.meta_path(cell).read_bytes()
    out = _revise(design, store, protocol, cell)
    assert out.revised
    _revise(design, store, protocol, cell)  # twice: still untouched
    assert _fingerprint(store.capture_path(cell)) == before and store.meta_path(cell).read_bytes() == meta_before
    assert store.capture_path(cell).read_text() == DIRTY
    assert store.revised_path(cell) != store.capture_path(cell) and store.revised_path(cell).name == "q1__B__sonnet.revised.md"
    assert store.read_capture(cell) == DIRTY  # hash-verified read still passes


def test_revision_is_recorded_with_diff_reason_steps_and_post_audit(design, store, protocol):
    _setup(design, store, lambda r: DIRTY if r.cell_id == "q1__B__sonnet" else clean_script(r))
    cell = Cell("q1", "B", "sonnet")
    out = _revise(design, store, protocol, cell, reason="Wave 2 pass 1.")
    revised = store.revised_path(cell).read_text()
    assert "—" not in revised and "course materials" not in revised.lower() and "Notably" not in revised
    assert revised.startswith("Orbit's best claim is cover damages, Glimmer delivered")
    assert "1990–2000" in revised and "1990–2000 is a typo" in revised  # em dash between digits became an en dash
    assert "Glimmer's defense is strong" in revised  # robust -> strong; "Furthermore, " removed
    rec = json.loads(store.revision_meta_path(cell).read_text())
    assert rec["reason"].startswith("Wave 2 pass 1. Protocol violations in the verbatim capture:") and "em_dash" in rec["reason"]
    kinds = {s["kind"] for s in rec["steps"]}
    assert {"preamble_strip", "em_dash", "banned_phrase"} <= kinds
    assert rec["source_capture_sha256"] == store.read_meta(cell)["capture_sha256"]
    assert rec["revised_sha256"] == hashlib.sha256(revised.encode()).hexdigest()
    diff = store.revision_diff_path(cell).read_text()
    assert diff.startswith("--- captures/q1__B__sonnet.md") and "+++ revisions/q1__B__sonnet.revised.md" in diff and "-Course materials" in diff
    # what could not be fixed mechanically is left visible, not guessed
    assert any(u["phrase"] == "this raises important questions" for u in rec["unresolved"])
    assert "this raises important questions" in revised.lower()
    assert any("banned_phrases" in h for h in rec["needs_human_review"])
    assert json.loads(store.audit_path(cell, revised=True).read_text())["target"] == "revised"


def test_no_revision_file_when_the_capture_is_clean(design, store, protocol):
    _setup(design, store, clean_script)
    out = _revise(design, store, protocol, Cell("q1", "A", "sonnet"))
    assert not out.revised and "passes the audit" in out.reason and not store.revised_path(Cell("q1", "A", "sonnet")).exists()


def test_em_dash_rules():
    assert fix_em_dashes("a—b") == ("a, b", 1)
    assert fix_em_dashes("a — b") == ("a, b", 1)
    assert fix_em_dashes("pages 10—20") == ("pages 10–20", 1)
    assert fix_em_dashes("a—b", "en")[0] == "a – b"
    assert fix_em_dashes("trailing—")[0] == "trailing" and fix_em_dashes("—lead")[0] == "lead"
    assert fix_em_dashes("none") == ("none", 0)


def test_banned_phrase_fixes_are_deterministic_and_keep_grammar():
    banned = ["notably", "furthermore", "it is worth noting", "robust", "underscores", "this raises important questions"]
    text = "Notably, the rule applies. And it is worth noting that Glimmer lost. The test is Robust and this underscores it. " \
           "This raises important questions."
    out1, steps, unresolved = fix_banned_phrases(text, banned, {})
    out2, _, _ = fix_banned_phrases(text, banned, {})
    assert out1 == out2
    assert out1.startswith("The rule applies. And Glimmer lost. The test is Strong and this shows it.")
    assert [u["phrase"] for u in unresolved] == ["this raises important questions"]
    assert {s["phrase"] for s in steps} == {"notably", "it is worth noting", "robust", "underscores"}
    custom, _, _ = fix_banned_phrases("It is robust.", ["robust"], {"robust": "sturdy"})
    assert custom == "It is sturdy."


def test_strip_leading_preamble_only_removes_strict_meta_sentences():
    out, removed = strip_leading_preamble("Course materials are attached. Maysonia is common-law. Rest.")
    assert out == "Maysonia is common-law. Rest." and removed == ["Course materials are attached."]
    out, removed = strip_leading_preamble("Here is my answer. Let me write it. Glimmer breached.")
    assert out == "Glimmer breached." and len(removed) == 2
    keep = "I would hold that Glimmer breached."  # a legitimate first sentence is NOT deleted mechanically
    assert strip_leading_preamble(keep) == (keep, [])


def test_mcq_answer_block_extraction():
    pre = "I now have the materials. Let me work through them.\n1. Fact pattern A is about sprockets.\nThe answer is B.\n\n" + CLEAN_MCQ
    assert extract_mcq_block(pre, 5) == CLEAN_MCQ
    assert extract_mcq_block("1. A\n2. B\n", 5) is None


def test_mcq_commentary_is_removed_and_answers_are_unchanged(design, store, protocol):
    noisy = "I now have all the materials. Let me work through the questions.\n\n" + CLEAN_MCQ
    _setup(design, store, lambda r: noisy if r.cell_id == "mcq__B__sonnet" else clean_script(r))
    cell = Cell("mcq", "B", "sonnet")
    out = _revise(design, store, protocol, cell)
    assert out.revised and store.revised_path(cell).read_text() == CLEAN_MCQ
    assert store.capture_path(cell).read_text() == noisy and out.post_audit["passed"]


def test_over_ceiling_needs_the_llm_pass_and_stays_flagged_without_it(design, store, protocol):
    long = " ".join(["Glimmer breached the contract."] * 160)
    _setup(design, store, lambda r: long if r.cell_id == "q1__A__sonnet" else clean_script(r))
    cell = Cell("q1", "A", "sonnet")
    out = _revise(design, store, protocol, cell)
    assert not out.revised and "no deterministic fix" in out.reason and any("word_count" in h for h in out.human_review)


def test_llm_revision_is_constrained_logged_and_accepted_when_clean(design, store, protocol):
    long = " ".join(["Glimmer breached the contract."] * 160)
    _setup(design, store, lambda r: long if r.cell_id == "q1__A__sonnet" else clean_script(r))
    cell = Cell("q1", "A", "sonnet")
    trimmed = " ".join(["Glimmer breached the contract."] * 135)
    fake = FakeProvider("anthropic", script=lambda r: trimmed)
    out = _revise(design, store, protocol, cell, use_llm=True, provider=fake)
    assert out.revised and out.llm["accepted"] and out.post_audit["passed"]
    req = fake.requests[0]
    assert "Fix ONLY the violations" in req.user and "exceeds the ceiling of 600" in req.user and req.cell_id == "q1__A__sonnet__revise"
    rec = json.loads(store.revision_meta_path(cell).read_text())
    assert rec["llm"]["used"] and rec["llm"]["usage"]["calls"] == 1 and rec["llm"]["prompt_sha256"] and rec["llm"]["violations_sent"]
    assert store.revised_path(cell).read_text() == trimmed and store.capture_path(cell).read_text() == long


def test_llm_revision_that_adds_authority_is_rejected(design, store, protocol):
    long = " ".join(["Glimmer breached the contract."] * 160)
    _setup(design, store, lambda r: long if r.cell_id == "q1__A__sonnet" else clean_script(r))
    cell = Cell("q1", "A", "sonnet")
    sneaky = " ".join(["Glimmer breached the contract."] * 135) + " As Hadley v. Baxendale held, damages must be foreseeable."
    out = _revise(design, store, protocol, cell, use_llm=True, provider=FakeProvider("anthropic", script=lambda r: sneaky))
    assert not out.revised and out.llm["accepted"] is False and "absent from the original" in out.llm["rejection_reason"]
    assert not store.revised_path(cell).exists()


def test_new_authority_tokens():
    assert new_authority_tokens("Glimmer lost in 2020.", "Glimmer lost in 2020.") == []
    assert "Hadley" in new_authority_tokens("Glimmer lost.", "Glimmer lost. See Hadley v. Baxendale, 156 Eng. Rep. 145.")
    assert new_authority_tokens("The Rule applies. Glimmer lost.", "Glimmer lost. The rule applies.") == []


def test_citation_flags_are_never_revised_away(design, store, protocol):
    text = CLEAN_ESSAY + " See Fakecase v. Nothing, 999 U.S. 999 (2099)."
    _setup(design, store, lambda r: text if r.cell_id == "q1__A__sonnet" else clean_script(r))
    cell = Cell("q1", "A", "sonnet")
    hook = lambda t: {"citations": [{"flags": ["fabricated_suspect"]}], "fabricated_suspects": [1], "verified": True}  # noqa: E731
    out = _revise(design, store, protocol, cell, use_llm=True, provider=FakeProvider("anthropic"), citations=hook)
    assert not out.revised and any(h.startswith("citations") for h in out.human_review)
    assert out.llm == {"used": False}  # nothing for the LLM to fix: a fabricated-suspect cite is for a human
