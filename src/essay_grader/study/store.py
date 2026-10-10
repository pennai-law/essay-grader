"""On-disk layout of a study run, and verbatim capture.

    <out>/<study_id>/
        study.lock.json            design hash, protocol fingerprint, tool version (written at first run)
        prompts/<cell>.prompt.txt  the rendered protocol prompt that was sent (materials are manifested, not copied)
        captures/<cell>.md         the model's output EXACTLY as returned (bytes, no newline translation), read-only
        captures/<cell>.meta.json  request metadata (see runner.py)
        captures/<cell>.error.json a failed/refused cell (retried on resume); no capture exists
        audits/<cell>.audit.json   per-cell audit of the capture (and <cell>.revised.audit.json of the revision)
        revisions/<cell>.revised.md / .revision.json / .diff     second, separately recorded pass
        package/deliver/           ONLY what would be handed to the registrar side (no arm in any name)
        package/manifest.private.json
        methodology/               record generator output (per-exam-summary.csv, decision-log.md, ...)
        scoring/                   scoring analysis
        run-summary.json
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .design import Cell, Design


class CaptureTamperedError(RuntimeError):
    """The capture file on disk no longer matches the hash recorded when it was captured."""


class StaleCaptureError(RuntimeError):
    """A captured cell was produced by a different prompt, materials or model than the current design."""


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class StudyStore:
    def __init__(self, out_dir: str | Path, design: Design):
        self.design = design
        self.root = Path(out_dir) / design.study_id

    # ---- paths ---------------------------------------------------------------------------------------------------
    def d(self, name: str) -> Path:
        p = self.root / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def prompt_path(self, cell: Cell) -> Path:
        return self.d("prompts") / f"{cell.id}.prompt.txt"

    def capture_path(self, cell: Cell) -> Path:
        return self.d("captures") / f"{cell.id}.md"

    def meta_path(self, cell: Cell) -> Path:
        return self.d("captures") / f"{cell.id}.meta.json"

    def error_path(self, cell: Cell) -> Path:
        return self.d("captures") / f"{cell.id}.error.json"

    def audit_path(self, cell: Cell, revised: bool = False) -> Path:
        return self.d("audits") / f"{cell.id}{'.revised' if revised else ''}.audit.json"

    def revised_path(self, cell: Cell) -> Path:
        return self.d("revisions") / f"{cell.id}.revised.md"

    def revision_meta_path(self, cell: Cell) -> Path:
        return self.d("revisions") / f"{cell.id}.revision.json"

    def revision_diff_path(self, cell: Cell) -> Path:
        return self.d("revisions") / f"{cell.id}.diff"

    # ---- capture -------------------------------------------------------------------------------------------------
    def write_capture(self, cell: Cell, text: str, meta: dict) -> None:
        """Write the output byte-for-byte (no strip, no newline translation); mark it read-only; then write the metadata.
        A cell counts as captured only once its meta file exists (so a crash between the two retries the cell)."""
        data = text.encode("utf-8")
        path = self.capture_path(cell)
        if path.exists():
            raise FileExistsError(f"refusing to overwrite verbatim capture {path}")
        tmp = path.with_suffix(".md.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        meta = {**meta, "capture_sha256": sha256_bytes(data), "capture_bytes": len(data)}
        self.meta_path(cell).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.error_path(cell).unlink(missing_ok=True)

    def write_error(self, cell: Cell, info: dict) -> None:
        self.error_path(cell).write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def is_captured(self, cell: Cell) -> bool:
        return self.meta_path(cell).is_file() and self.capture_path(cell).is_file()

    def read_meta(self, cell: Cell) -> dict:
        return json.loads(self.meta_path(cell).read_text(encoding="utf-8"))

    def read_capture(self, cell: Cell, *, verify: bool = True) -> str:
        data = self.capture_path(cell).read_bytes()
        if verify:
            want = self.read_meta(cell).get("capture_sha256")
            if want and sha256_bytes(data) != want:
                raise CaptureTamperedError(f"{self.capture_path(cell)} does not match its recorded sha256; the verbatim "
                                           f"capture was modified after capture")
        return data.decode("utf-8")

    def captured_cells(self) -> list[Cell]:
        return [c for c in self.design.cells() if self.is_captured(c)]

    def final_text(self, cell: Cell, *, prefer_revised: bool = True) -> tuple[str, str]:
        """(text, source) where source is 'revised' or 'capture'. Packaging builds from the revised pass when one exists."""
        rp = self.revised_path(cell)
        if prefer_revised and rp.is_file():
            return rp.read_text(encoding="utf-8"), "revised"
        return self.read_capture(cell), "capture"
