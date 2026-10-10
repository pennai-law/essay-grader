import asyncio
from types import SimpleNamespace as NS

from essay_grader.calibrate import calibrate
from essay_grader.fake import FakeLLM
from essay_grader.graders.claude_grader import GradeOutput
from essay_grader.harden import HardenConfig, harden, render_markdown
from essay_grader.ingest import Answer
from essay_grader.llm import AnthropicLLM


class FakeMessages:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kw):
        self.kwargs = kw
        block = NS(type="tool_use", input={"judgments": []})
        return NS(content=[block], stop_reason="tool_use", model="claude-opus-5-5-20260101",
                  usage=NS(input_tokens=5, output_tokens=2, cache_read_input_tokens=100, cache_creation_input_tokens=0))


def test_anthropic_request_shape():
    msgs = FakeMessages()
    llm = AnthropicLLM("claude-opus-5-5", client=NS(messages=msgs), retry_delay=0)
    res = asyncio.run(llm.call(prefix="P", user="U", tool_name="t", tool_description="d", schema=GradeOutput))
    k = msgs.kwargs
    assert k["model"] == "claude-opus-5-5" and "temperature" not in k
    assert k["tool_choice"] == {"type": "tool", "name": "t"}
    assert k["tools"][0]["name"] == "t" and "input_schema" in k["tools"][0]
    assert k["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert res.served_model.startswith("claude-opus-5-5-") and res.usage.cache_read_tokens == 100


def test_harden_offline(draft, exam_text):
    rep = asyncio.run(harden(draft, exam_text, "syllabus", FakeLLM(), HardenConfig(n_synthetic=11, k=3, concurrency=3)))
    archs = {a["archetype"] for a in rep["synthetic_answers"]}
    assert len(archs) == 11
    assert {f["source"] for f in rep["findings"]} >= {"critique"}
    assert all(len(r["criteria"]) == 3 for r in rep["stress"])
    md = render_markdown(rep)
    assert "Archetype outcomes" in md and "Proposed amendment" in md


def test_calibrate_creates_unlocked_new_version(locked, exam_text):
    samples = [Answer("S001", "Q1", "text one", "S001.txt"), Answer("S002", "Q1", "text two", "S002.txt")]
    new, rep = asyncio.run(calibrate(locked, exam_text, samples, FakeLLM()))
    assert new.version == 2 and not new.locked
    assert new.calibration.sample_ids == ["S001", "S002"] and new.calibration.parent_hash == locked.content_hash
    assert any(r.startswith("[fake ruling]") for c in new.questions[0].criteria for r in c.edge_case_rulings)
    assert rep["calibration_sample_ids"] == ["S001", "S002"]
