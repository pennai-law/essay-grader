"""Protocol assets: prompt templates loaded by path, recorded by sha256, with a prompt-evolution discipline.

Mirrors exam-taker's rule (CLAUDE.md, "On any prompt-template change"): a template change is a methodological event. Here
it is enforced in code. `protocol.yaml` lists the templates and an `evolution` log; the newest log entry (== `prompt_version`)
must record the sha256 of every template. If a template on disk no longer hashes to that record, loading fails with
`ProtocolDriftError` until someone bumps `prompt_version` and adds an evolution entry. Penn exam content never lives here:
a protocol directory holds only templates, and is given by path.

    protocol.yaml
        protocol_id: my-protocol
        prompt_version: 2
        templates: {essay: prompt_template_essay.txt, mcq: prompt_template_mcq.txt}
        evolution:
          - {version: 1, date: 2026-10-01, summary: ..., rationale: ..., template_sha256: {essay: ..., mcq: ...}}
          - {version: 2, ...}
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

QUESTION_PLACEHOLDER = re.compile(r"\[PASTE THE FULL EXAM QUESTION HERE[^\]]*\]")
COURSE_PLACEHOLDER = "[COURSE NAME]"
_STYLE_BLOCK = re.compile(r"<(style[a-z_]*)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_QUOTED = re.compile(r"[\"“]([^\"”]+)[\"”]")


class ProtocolError(Exception):
    pass


class ProtocolDriftError(ProtocolError):
    """A template changed without a `prompt_version` bump and an evolution entry."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class EvolutionEntry:
    version: int
    date: str
    summary: str
    rationale: str
    template_sha256: dict[str, str]
    triggered_by: str = ""
    affected: str = ""


@dataclass(frozen=True)
class Template:
    key: str
    path: Path
    text: str
    sha256: str


def parse_banned_phrases(template_text: str) -> list[str]:
    """Quoted phrases on the line(s) of a `<style...>` block that contain "stock phrases" (exam-taker's convention).

    `"it is worth noting,"` becomes `it is worth noting` (trailing punctuation inside the quotes is dropped)."""
    out: list[str] = []
    for _tag, body in _STYLE_BLOCK.findall(template_text):
        for line in body.splitlines():
            if "stock phrases" in line.lower():
                after = re.split(r"stock phrases\s*:?", line, maxsplit=1, flags=re.IGNORECASE)[-1]
                for m in _QUOTED.findall(after):
                    phrase = m.strip().rstrip(",.;:").strip()
                    if phrase and phrase.lower() not in (p.lower() for p in out):
                        out.append(phrase)
    return out


@dataclass
class Protocol:
    protocol_id: str
    prompt_version: int
    templates: dict[str, Template]
    evolution: list[EvolutionEntry]
    root: Path
    banned_phrase_fixes: dict[str, str] = field(default_factory=dict)

    # ---- loading -----------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, directory: str | Path, *, verify: bool = True) -> "Protocol":
        root = Path(directory).resolve()
        spec_path = root / "protocol.yaml"
        if not spec_path.is_file():
            raise ProtocolError(f"{spec_path} not found (a protocol directory needs protocol.yaml)")
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        try:
            templates: dict[str, Template] = {}
            for key, rel in dict(spec["templates"]).items():
                p = (root / rel).resolve()
                if not p.is_relative_to(root):
                    raise ProtocolError(f"template {key!r} points outside the protocol directory: {rel}")
                if not p.is_file():
                    raise ProtocolError(f"template {key!r} not found: {p}")
                text = p.read_text(encoding="utf-8")
                templates[str(key)] = Template(str(key), p, text, sha256_text(text))
            evo = [EvolutionEntry(version=int(e["version"]), date=str(e.get("date", "")), summary=str(e.get("summary", "")),
                                  rationale=str(e.get("rationale", "")),
                                  template_sha256={str(k): str(v) for k, v in dict(e.get("template_sha256") or {}).items()},
                                  triggered_by=str(e.get("triggered_by", "")), affected=str(e.get("affected", "")))
                   for e in spec.get("evolution") or []]
            proto = cls(protocol_id=str(spec["protocol_id"]), prompt_version=int(spec["prompt_version"]), templates=templates,
                        evolution=evo, root=root,
                        banned_phrase_fixes={str(k).lower(): str(v) for k, v in dict(spec.get("banned_phrase_fixes") or {}).items()})
        except KeyError as e:
            raise ProtocolError(f"{spec_path}: missing required field {e}") from e
        if verify:
            proto.verify()
        return proto

    # ---- the discipline ----------------------------------------------------------------------------------------
    def verify(self) -> None:
        if not self.evolution:
            raise ProtocolError("protocol.yaml has no `evolution` log; record at least the current version")
        versions = [e.version for e in self.evolution]
        if versions != sorted(set(versions)):
            raise ProtocolError(f"evolution versions must be strictly increasing, got {versions}")
        latest = self.evolution[-1]
        if latest.version != self.prompt_version:
            raise ProtocolError(f"prompt_version is {self.prompt_version} but the newest evolution entry is version "
                                f"{latest.version}; bump both together")
        for key, t in self.templates.items():
            want = latest.template_sha256.get(key)
            if want is None:
                raise ProtocolDriftError(f"evolution entry v{latest.version} records no sha256 for template {key!r}; "
                                         f"add `{key}: {t.sha256}`")
            if want != t.sha256:
                raise ProtocolDriftError(
                    f"template {key!r} changed (sha256 {t.sha256[:12]}..., recorded {want[:12]}... for prompt_version "
                    f"{self.prompt_version}). Bump prompt_version and add an evolution entry with template_sha256 "
                    f"`{key}: {t.sha256}` before running (exam-taker prompt-evolution discipline).")

    def current_hashes(self) -> dict[str, str]:
        return {k: t.sha256 for k, t in self.templates.items()}

    def fingerprint(self) -> dict:
        return {"protocol_id": self.protocol_id, "prompt_version": self.prompt_version, "template_sha256": self.current_hashes()}

    # ---- rendering ---------------------------------------------------------------------------------------------
    def render(self, kind: str, *, course: str, question_text: str) -> str:
        if kind not in self.templates:
            raise ProtocolError(f"protocol has no {kind!r} template (has {sorted(self.templates)})")
        text = self.templates[kind].text
        if not QUESTION_PLACEHOLDER.search(text):
            raise ProtocolError(f"template {kind!r} has no [PASTE THE FULL EXAM QUESTION HERE] placeholder")
        text = text.replace(COURSE_PLACEHOLDER, course)
        return QUESTION_PLACEHOLDER.sub(lambda _m: question_text.strip("\n"), text, count=1)

    def banned_phrases(self, kind: str) -> list[str]:
        return parse_banned_phrases(self.templates[kind].text)
