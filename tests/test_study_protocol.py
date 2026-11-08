"""Protocol assets: path loading, sha256 recording, prompt-version discipline, banned-phrase parsing."""
import shutil

import pytest
import yaml

from essay_grader.study.protocol import (Protocol, ProtocolDriftError, ProtocolError, parse_banned_phrases, sha256_text)
from test_study_helpers import FIX


def _copy(tmp_path):
    dst = tmp_path / "protocol"
    shutil.copytree(FIX / "protocol", dst)
    return dst


def test_loads_from_a_path_and_records_hashes():
    p = Protocol.load(FIX / "protocol")
    assert p.protocol_id == "fictional-widget-protocol" and p.prompt_version == 2
    assert set(p.templates) == {"essay", "mcq"}
    assert p.current_hashes()["essay"] == sha256_text((FIX / "protocol" / "prompt_template_essay.txt").read_text())
    assert p.evolution[-1].template_sha256 == p.current_hashes()


def test_template_change_without_version_bump_is_refused(tmp_path):
    d = _copy(tmp_path)
    t = d / "prompt_template_essay.txt"
    t.write_text(t.read_text() + "\nOne more instruction.\n")
    with pytest.raises(ProtocolDriftError) as e:
        Protocol.load(d)
    assert "Bump prompt_version" in str(e.value) and "essay" in str(e.value)


def test_bump_with_evolution_entry_is_accepted(tmp_path):
    d = _copy(tmp_path)
    t = d / "prompt_template_essay.txt"
    t.write_text(t.read_text() + "\nOne more instruction.\n")
    spec = yaml.safe_load((d / "protocol.yaml").read_text())
    spec["prompt_version"] = 3
    hashes = {k: sha256_text((d / v).read_text()) for k, v in spec["templates"].items()}
    spec["evolution"].append({"version": 3, "date": "2026-10-01", "summary": "extra", "rationale": "test", "template_sha256": hashes})
    (d / "protocol.yaml").write_text(yaml.safe_dump(spec))
    assert Protocol.load(d).prompt_version == 3


def test_version_must_match_newest_evolution_entry(tmp_path):
    d = _copy(tmp_path)
    spec = yaml.safe_load((d / "protocol.yaml").read_text())
    spec["prompt_version"] = 5
    (d / "protocol.yaml").write_text(yaml.safe_dump(spec))
    with pytest.raises(ProtocolError, match="prompt_version"):
        Protocol.load(d)


def test_template_outside_protocol_dir_is_refused(tmp_path):
    d = _copy(tmp_path)
    (tmp_path / "evil.txt").write_text("x")
    spec = yaml.safe_load((d / "protocol.yaml").read_text())
    spec["templates"]["essay"] = "../evil.txt"
    (d / "protocol.yaml").write_text(yaml.safe_dump(spec))
    with pytest.raises(ProtocolError, match="outside"):
        Protocol.load(d)


def test_banned_phrases_parsed_from_style_block():
    p = Protocol.load(FIX / "protocol")
    essay, mcq = p.banned_phrases("essay"), p.banned_phrases("mcq")
    assert "it is worth noting" in essay and "robust" in essay and "in conclusion" in essay
    assert "in conclusion" not in mcq and "robust" in mcq  # per-template lists, like exam-taker's two templates
    assert all(not x.endswith((",", ".")) for x in essay)


def test_parse_banned_phrases_handles_curly_quotes_and_other_lines():
    txt = '<style>\nTake "positions" on close calls.\nDo not use the following stock phrases: “foo bar,” "baz."\n</style>'
    assert parse_banned_phrases(txt) == ["foo bar", "baz"]


def test_render_substitutes_course_and_question():
    p = Protocol.load(FIX / "protocol")
    out = p.render("essay", course="Widget Law", question_text="QUESTION TEXT HERE")
    assert "final exam in Widget Law" in out and "QUESTION TEXT HERE" in out
    assert "[COURSE NAME]" not in out and "PASTE THE FULL" not in out
    with pytest.raises(ProtocolError):
        p.render("nope", course="x", question_text="y")
