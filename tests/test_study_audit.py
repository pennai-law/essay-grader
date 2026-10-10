"""Automated audit (NEXT_SESSION_GUIDE Step 4 ported): em dashes, banned phrases, ceilings, preambles, leaks, citations hook."""
import json

from essay_grader.study.audit import audit_study, audit_text
from essay_grader.study.design import Cell
from test_study_helpers import CLEAN_ESSAY, CLEAN_MCQ, clean_script, design, design_path, protocol, run, store  # noqa: F401


def _a(design, protocol, text, comp="q1", **kw):
    c = design.component(comp)
    kw.setdefault("citations", False)
    return audit_text(text, c, banned=protocol.banned_phrases(c.kind), cell_id="x", **kw)


def _checks(res):
    return {f.check for f in res.violations}


def test_clean_essay_and_mcq_pass(design, protocol):
    r = _a(design, protocol, CLEAN_ESSAY)
    assert r.passed and r.checks["em_dash"] == "pass" and r.checks["banned_phrases"] == "pass"
    assert _a(design, protocol, CLEAN_MCQ, "mcq").passed


def test_em_dash_detected_but_en_dash_allowed(design, protocol):
    r = _a(design, protocol, "Glimmer lost—badly. The range 1990–2000 is fine.")
    assert _checks(r) == {"em_dash"} and r.violations[0].line == 1 and "—" in r.violations[0].evidence
    assert _a(design, protocol, "The range 1990–2000 is fine.").passed


def test_banned_phrases_detected_case_insensitively(design, protocol):
    r = _a(design, protocol, "Notably, Orbit wins. It Is Worth Noting that the rule is Robust. Moreover, fine. In conclusion, done.")
    phrases = sorted(f.message.split("'")[1] for f in r.violations if f.check == "banned_phrases")
    assert phrases == ["in conclusion", "it is worth noting", "moreover", "notably", "robust"]


def test_over_ceiling_is_a_violation_and_target_band_is_a_warning(design, protocol):
    over = " ".join(["word"] * 601) + "."
    r = _a(design, protocol, over)
    assert "word_count" in _checks(r) and "601 words exceeds the ceiling of 600" in r.violations[0].message
    ok = _a(design, protocol, " ".join(["word"] * 600) + ".")
    assert "word_count" not in _checks(ok)  # exactly at the ceiling: allowed (the band, not the ceiling, flags it)
    short = _a(design, protocol, " ".join(["word"] * 100) + ".")
    assert short.passed and any(f.check == "word_count" and f.severity == "warn" for f in short.findings)
    mid = _a(design, protocol, " ".join(["word"] * 540) + ".")
    assert mid.checks["word_count"] == "pass"  # 90% of 600 is inside 85-95%


def test_wc_w_semantics(design, protocol):
    assert _a(design, protocol, "one two\nthree\t four  five").words == 5


def test_format_violations_in_essays(design, protocol):
    r = _a(design, protocol, "# Heading\n\n- bullet one\n\n**Bold** text and *Palsgraf* italics and _Erie_ too.\n\n1. numbered")
    msgs = " | ".join(f.message for f in r.violations if f.check == "format")
    assert "header / bullet" in msgs and "bold" in msgs and "italics" in msgs


def test_preamble_first_sentence_and_the_fused_form(design, protocol):
    fused = "Course materials are attached. Glimmer breached by delivering only part of the order."
    r = _a(design, protocol, fused)
    assert {"materials_leak"} <= _checks(r)  # the L-043 family
    p = _a(design, protocol, "Here is my answer to the exam question.\n\nGlimmer breached.")
    assert "preamble" in _checks(p) and p.violations[0].line == 1
    m = _a(design, protocol, "I now have the full picture. Let me write the exam answer. Glimmer breached.")
    assert "preamble" in _checks(m)


def test_materials_condition_tells_are_caught_anywhere_in_the_text(design, protocol):
    for t in ("Based on the provided outline, the rule is X.", "Without any course materials I rely on memory.",
              "I have reviewed the attached syllabus. Glimmer breached."):
        r = _a(design, protocol, "Glimmer breached. " * 5 + t)
        assert "materials_leak" in _checks(r), t
    assert "materials_leak" not in _checks(_a(design, protocol, "The rule in this course is that Glimmer breached."))


def test_mcq_letters_only_format(design, protocol):
    r = _a(design, protocol, "Here are my answers.\n" + CLEAN_MCQ, "mcq")
    assert "format" in _checks(r) and "non-answer line" in r.violations[0].message
    short = _a(design, protocol, "1. A\n2. B\n", "mcq")
    assert "format" in _checks(short) and "expected answers 1..5" in short.violations[0].message
    assert "format" in _checks(_a(design, protocol, "1. A\n2. B\n3. B\n4. A\n5. A\n6. C\n", "mcq"))
    assert "format" in _checks(_a(design, protocol, "1. A\n3. B\n2. B\n4. A\n5. A\n", "mcq"))  # out of order


def test_truncated_or_empty_output_is_a_violation(design, protocol):
    assert "completeness" in _checks(_a(design, protocol, CLEAN_ESSAY, truncated=True))
    assert "completeness" in _checks(_a(design, protocol, "   "))


# ---- citations hook ------------------------------------------------------------------------------------------
def test_citations_hook_is_skipped_with_a_note_when_the_module_is_absent(design, protocol, monkeypatch):
    import essay_grader.study.audit as audit_mod

    monkeypatch.setattr(audit_mod, "citations_hook", lambda: None)
    r = _a(design, protocol, CLEAN_ESSAY, citations=True)
    assert r.citations["status"] == "skipped" and "not available" in r.citations["note"] and r.passed
    assert r.to_dict()["citations"]["note"]


def test_citations_hook_called_and_suspects_fail_the_audit(design, protocol):
    seen = []

    def hook(text):
        seen.append(text)
        return {"citations": [{"flags": ["fabricated_suspect"]}], "fabricated_suspects": [1], "verified": True}

    r = _a(design, protocol, CLEAN_ESSAY, citations=hook)
    assert seen == [CLEAN_ESSAY] and r.citations["status"] == "flagged" and "citations" in _checks(r)


def test_unverifiable_citations_are_informational_and_other_flags_are_warnings(design, protocol):
    r = _a(design, protocol, CLEAN_ESSAY, citations=lambda t: {"citations": [{"flags": ["unverifiable"]}], "verified": False})
    assert r.passed and r.citations["status"] == "ok"
    r = _a(design, protocol, CLEAN_ESSAY, citations=lambda t: {"citations": [{"flags": ["name_mismatch"]}], "verified": True})
    assert r.passed and r.citations["status"] == "review" and any(f.check == "citations" and f.severity == "warn" for f in r.findings)


def test_citations_hook_errors_are_recorded_not_raised(design, protocol):
    def boom(t):
        raise RuntimeError("network down")

    r = _a(design, protocol, CLEAN_ESSAY, citations=boom)
    assert r.citations["status"] == "error" and "network down" in r.citations["note"] and r.passed


def test_real_citations_module_if_present_runs_offline(design, protocol):
    r = _a(design, protocol, CLEAN_ESSAY + " See Palsgraf v. Long Island R.R. Co., 248 N.Y. 339 (1928).", citations=True)
    assert r.citations["status"] in {"skipped", "ok", "review", "flagged", "error"}  # whichever the sibling module reports; never raises


# ---- per-cell audit JSON ---------------------------------------------------------------------------------------
def test_audit_study_writes_one_json_per_cell(design, store, protocol):
    bad = {"q1__B__sonnet": "Notably—Glimmer breached. " + CLEAN_ESSAY}
    from essay_grader.study.providers import FakeProvider

    fake = FakeProvider("anthropic", script=lambda r: bad.get(r.cell_id) or clean_script(r))
    run(design, store, providers={"anthropic": fake})
    results = audit_study(design, store, protocol, citations=False)
    assert len(results) == 6
    j = json.loads(store.audit_path(Cell("q1", "B", "sonnet")).read_text())
    assert j["passed"] is False and j["target"] == "capture" and j["checks"]["em_dash"] == "fail" and j["checks"]["banned_phrases"] == "fail"
    assert j["audited_sha256"] and j["findings"] and j["citations"]["status"] == "skipped"
    ok = json.loads(store.audit_path(Cell("q1", "A", "sonnet")).read_text())
    assert ok["passed"] is True
    assert store.audit_path(Cell("q1", "A", "sonnet")).name == "q1__A__sonnet.audit.json"
