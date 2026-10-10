"""Offline fakes for --dry-run and tests: canned, deterministic, no network. Scores are meaningless."""
from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel

from .llm import CallResult, Usage


def _u(text: str, salt: str = "") -> float:
    return int(hashlib.sha256((salt + text).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def quality(answer: str) -> float:
    """Pseudo 'answer quality' in [0,1], shared by both fake graders so they mostly agree."""
    return _u(answer)


def _first_quote(answer: str) -> list[str]:
    words = answer.split()
    return [" ".join(words[:8])] if words else []


class FakeLLM:
    """Stands in for AnthropicLLM; dispatches on `hint['kind']`."""

    def __init__(self, model: str = "fake-claude"):
        self.model = model
        self.calls = 0

    async def call(self, *, prefix: str, user: str, tool_name: str, tool_description: str,
                   schema: type[BaseModel], max_tokens: int = 4096, hint: dict | None = None) -> CallResult:
        self.calls += 1
        h = hint or {}
        kind = h.get("kind")
        if kind == "grade":
            ans = h["answer"]
            q = quality(ans)
            js = []
            for c in h["question"]["criteria"]:
                j = {"criterion_id": c["id"], "evidence": _first_quote(ans), "rationale": "canned fake rationale",
                     "confidence": 0.4 + 0.6 * _u(ans, c["id"])}
                if c["kind"] == "binary":
                    j["met"] = q + 0.2 * (_u(ans, c["id"]) - 0.5) > 0.5
                elif c["kind"] == "choice":
                    lv = sorted(c["levels"], key=lambda x: x["points"])
                    j["level"] = lv[round(q * (len(lv) - 1))]["label"]
                else:
                    s = c["scale"]
                    j["score"] = round((s["min"] + q * (s["max"] - s["min"])) * 2) / 2
                js.append(j)
            data = {"judgments": js}
        elif kind == "critique":
            cid = h["rubric"]["questions"][0]["criteria"][0]["id"]
            data = {"findings": [{"category": "missing_policy", "criterion_id": None,
                                  "issue": "[fake] No policy for answers that list issues without analysis.",
                                  "evidence": "[fake evidence]",
                                  "amendment": {"type": "add_policy", "text": "Issue-spotting without analysis earns no analysis credit."}},
                                 {"category": "ambiguous", "criterion_id": cid, "issue": "[fake] Criterion wording is ambiguous.",
                                  "evidence": "[fake evidence]",
                                  "amendment": {"type": "edge_case_ruling", "criterion_id": cid, "text": "[fake ruling]"}}]}
        elif kind == "synth":
            data = {"answer": (f"[fake {h['archetype']} v{h['variant']}] " + "The offer was accepted by performance and consideration exists. " * 6).strip()}
        elif kind == "stress":
            data = {"findings": [{"flag_id": f["flag_id"], "explanation": "[fake]",
                                  "amendment": {"type": "edge_case_ruling", "criterion_id": f.get("criterion_id"),
                                                "text": "[fake amendment]"}} for f in h["flags"]]}
        elif kind == "calibrate":
            cid = h["question"]["criteria"][0]["id"]
            data = {"open_questions": [{"description": "[fake] Some students argued an alternative theory.",
                                        "sample_ids": h["sample_ids"][:2], "criterion_id": cid,
                                        "proposed_ruling": "[fake ruling] Credit the alternative theory if reasoned."}]}
        else:
            raise ValueError(f"FakeLLM: unknown hint kind {kind!r}")
        return CallResult(schema.model_validate(data), Usage(input_tokens=100, output_tokens=50, calls=1), self.model)


async def fake_decisions_transport(path: str, body: dict) -> dict:
    """Mimics the Decisions response shape (see openai_decisions_grader.py). Answers are returned reversed to exercise name matching."""
    text = body["input"][0]["content"][-1]["text"]
    q = quality(text)
    answers = []
    for qq in body["questions"]:
        name = qq["name"]
        noise = _u(text, "o" + name) - 0.5
        conf = round(0.4 + 0.6 * _u(text, "c" + name), 3)
        if qq["type"] == "predicate":
            answers.append({"type": "predicate", "name": name, "probability": min(1, max(0, q + 0.4 * noise))})
        elif qq["type"] == "choice":
            opts = qq["choices"]
            k = min(len(opts) - 1, max(0, round((q + 0.5 * noise) * (len(opts) - 1))))
            answers.append({"type": "choice", "name": name, "choice": opts[k]["value"], "confidence": conf,
                            "probabilities": [{"value": o["value"], "probability": 1.0 if i == k else 0.0}
                                              for i, o in enumerate(opts)]})
        else:
            lv = qq["levels"]
            idx = round(min(1, max(0, q + 0.3 * noise)) * (len(lv) - 1) * 2) / 2
            k = min(len(lv) - 1, round(idx))
            answers.append({"type": "score", "name": name, "score": idx, "confidence": conf,
                            "probabilities": [{"value": i, "label": l["label"], "probability": 1.0 if i == k else 0.0}
                                              for i, l in enumerate(lv)]})
    return {"id": "fake", "model": body["model"], "answers": answers[::-1], "usage": {"input_tokens": 80}}
