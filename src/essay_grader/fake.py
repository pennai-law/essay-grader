"""Offline fakes for --dry-run and tests: canned, deterministic, no network. Scores are meaningless."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace as NS
from typing import Any

from .llm import AnthropicLLM


def _u(text: str, salt: str = "") -> float:
    return int(hashlib.sha256((salt + text).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def quality(answer: str) -> float:
    """Pseudo 'answer quality' in [0,1], shared by both fake graders so they mostly agree."""
    return _u(answer)


def _first_quote(answer: str) -> list[str]:
    words = answer.split()
    return [" ".join(words[:8])] if words else []


_ALLOWED_REQUEST_KEYS = {"model", "max_tokens", "system", "messages", "output_config", "betas", "fallbacks"}


def validate_request(req: dict) -> None:
    """Mirror the API's rules for the Claude 5.5 models; a violation is the offline equivalent of HTTP 400."""
    from .llm import FALLBACK_BETA, RequestShapeError

    for bad in ("tool_choice", "tools", "temperature", "top_p", "top_k", "thinking"):
        if bad in req:
            raise RequestShapeError(f"400: `{bad}` is not accepted for this model")
    extra = set(req) - _ALLOWED_REQUEST_KEYS
    if extra:
        raise RequestShapeError(f"unexpected request keys {sorted(extra)}")
    oc = req.get("output_config") or {}
    fmt = oc.get("format") or {}
    if fmt.get("type") != "json_schema" or not isinstance(fmt.get("schema"), dict):
        raise RequestShapeError("output_config.format must be a json_schema")
    if oc.get("effort") not in ("low", "medium", "high"):
        raise RequestShapeError("output_config.effort must be set explicitly")
    if FALLBACK_BETA not in (req.get("betas") or []) or req.get("fallbacks") != "default":
        raise RequestShapeError("server-side fallback beta not enabled")
    if not isinstance(req.get("max_tokens"), int):
        raise RequestShapeError("max_tokens required")


def _req_chars(req: dict) -> int:
    return sum(len(b["text"]) for b in req["system"]) + sum(len(m["content"]) for m in req["messages"])


class FakeLLM(AnthropicLLM):
    """Offline stand-in. Subclasses AnthropicLLM so the REAL request builder runs; only the transport is faked.

    Every request is validated against the API's shape rules (`validate_request`) and recorded in `self.requests`.
    Canned content is dispatched on `hint['kind']`. Usage is derived from sizes (chars/4) so budgets behave realistically.
    `refuse_kinds`: hint kinds answered with stop_reason="refusal".
    """

    def __init__(self, model: str = "claude-opus-5-5", *, refuse_kinds: set[str] | None = None,
                 refuse_all: bool = False):
        super().__init__(model, client=object(), retry_delay=0, attempts=2)
        self.calls = 0
        self.requests: list[dict] = []
        self.refuse_kinds, self.refuse_all = set(refuse_kinds or ()), refuse_all

    async def _create(self, request: dict, hint: dict | None) -> Any:
        validate_request(request)
        self.calls += 1
        self.requests.append(request)
        h = hint or {}
        in_tok = _req_chars(request) // 4
        if self.refuse_all or h.get("kind") in self.refuse_kinds:
            return NS(content=[], stop_reason="refusal", model=self.model,
                      usage=NS(input_tokens=in_tok, output_tokens=0, cache_read_input_tokens=0,
                               cache_creation_input_tokens=0))
        text = json.dumps(self._canned(h))
        return NS(content=[NS(type="text", text=text)], stop_reason="end_turn", model=self.model,
                  usage=NS(input_tokens=in_tok, output_tokens=len(text) // 4, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0))

    @staticmethod
    def _canned(h: dict) -> dict:
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
            return {"judgments": js}
        if kind == "critique":
            cid = h["criterion_ids"][0]
            return {"findings": [{"category": "missing_policy", "criterion_id": None,
                                  "issue": "[fake] No policy for answers that list issues without analysis.",
                                  "evidence": "[fake evidence]",
                                  "amendment": {"type": "add_policy", "text": "Issue-spotting without analysis earns no analysis credit."}},
                                 {"category": "ambiguous", "criterion_id": cid, "issue": "[fake] Criterion wording is ambiguous.",
                                  "evidence": "[fake evidence]",
                                  "amendment": {"type": "edge_case_ruling", "criterion_id": cid, "text": "[fake ruling]"}}]}
        if kind == "synth_all":
            return {"answers": [{"question_id": s["question_id"], "archetype": s["archetype"],
                                 "answer": (f"[fake {s['archetype']}] " + "The offer was accepted by performance and consideration exists. " * 6).strip()}
                                for s in h["specs"]]}
        if kind == "questionnaire":
            cids, answers = h["criterion_ids"], h["answers"]
            n = h["max_questions"] + 2
            qs = []
            for i in range(n):
                cid = cids[i % len(cids)] if i % 4 != 3 else None
                a = answers[i % len(answers)]["id"] if answers else None
                words = " ".join(f"fakeword{i}{x}" for x in "abcdefgh")
                cmax = h.get("criteria_max", {}).get(cid, 1.0) if cid else 1.0
                obs = (h.get("observed_points", {}).get(a, {}) or {}).get(cid, cmax / 2) if cid else 0.0

                def proj(after: float, cid=cid, obs=obs) -> list[dict]:  # deliberately sloppy numbers: the engine must clamp them
                    return [{"criterion_id": cid, "before_points": obs + 99, "after_points": after}] if cid else []

                opts = ([{"label": "adopt", "ruling": f"[fake ruling {i}] Apply the stated rule consistently.", "projected": proj(-3.0)},
                         {"label": "rewrite", "rewrite": {"target": "description", "text": f"[fake rewrite {i}]"}, "projected": proj(cmax * 5)},
                         {"label": "leave", "ruling": f"[fake ruling {i}b] Grade as written.", "projected": proj(obs)}] if cid else
                        [{"label": "strict", "ruling": f"[fake policy {i}] Rabbit-hole material earns no credit."},
                         {"label": "lenient", "rewrite": {"target": "policy:off_topic_policy", "text": f"[fake policy rewrite {i}]"}}])
                qs.append({"criterion_id": cid, "scenario": f"[fake] scenario {words}", "excerpt": "", "answer_id": a,
                           "why_it_matters": f"[fake] spread {words}", "severity": 1 + i % 5, "options": opts,
                           "recommended": opts[0]["label"]})
            if len(qs) > 2:  # a near-duplicate of the first question, to exercise dedupe
                d = dict(qs[0])
                d["severity"] = 1
                qs.append(d)
            return {"questions": qs}
        if kind == "calibrate":
            cid = h["criterion_ids"][0]
            return {"questions": [{"criterion_id": cid, "scenario": "[fake] Some students argued an alternative theory.",
                                   "excerpt": "", "answer_id": None, "why_it_matters": f"[fake] {len(h['sample_ids'])} sample answers",
                                   "severity": 3,
                                   "options": [{"label": "credit", "ruling": "[fake ruling] Credit the alternative theory if reasoned."},
                                               {"label": "no credit", "ruling": "[fake ruling] Do not credit the alternative theory."}],
                                   "recommended": "credit"}]}
        raise ValueError(f"FakeLLM: unknown hint kind {kind!r}")


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
