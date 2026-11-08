"""Shared helpers/fixtures for the study tests (fictional fixtures under tests/fixtures/study; fully offline)."""
import asyncio
import shutil
from pathlib import Path

import pytest
import yaml

from essay_grader.budget import Budget
from essay_grader.study.design import Design
from essay_grader.study.protocol import Protocol
from essay_grader.study.providers import FakeProvider
from essay_grader.study.runner import run_study
from essay_grader.study.store import StudyStore

FIX = Path(__file__).parent / "fixtures" / "study"
CLEAN_ESSAY = ("Orbit's best claim is cover damages. Glimmer breached by delivering only 200 of 500 sprockets. "
               "Orbit covered 300 at six dollars against a four dollar price. The closer question is impracticability. "
               "Here the flood closed Glimmer's only factory, which looks like a failed basic assumption, but Glimmer "
               "had to allocate capacity fairly and give notice. On these facts Orbit likely wins cover damages for the "
               "shortfall.")
CLEAN_MCQ = "1. B\n2. B\n3. B\n4. A\n5. A\n"


def make_study(tmp_path: Path, mutate=None) -> Path:
    """Copy the fixture tree into tmp_path and optionally mutate design.yaml (a dict) before use."""
    dst = tmp_path / "study"
    shutil.copytree(FIX, dst)
    p = dst / "design.yaml"
    if mutate:
        d = yaml.safe_load(p.read_text())
        mutate(d)
        p.write_text(yaml.safe_dump(d, sort_keys=False))
    return p


def multi_model(d: dict) -> None:
    d["models"].append({"key": "gpt", "provider": "openai", "model": "gpt-6-luna"})
    d["examids"] = {f"{a}/{m}": f"0{100 + 10 * i + j}" for i, a in enumerate("ASB") for j, m in enumerate(["sonnet", "gpt"])}


@pytest.fixture
def design_path(tmp_path):
    return make_study(tmp_path)


@pytest.fixture
def design(design_path):
    return Design.load(design_path)


@pytest.fixture
def protocol(design):
    return Protocol.load(design.path(design.protocol))


@pytest.fixture
def store(tmp_path, design):
    return StudyStore(tmp_path / "out", design)


def clean_script(req):
    return CLEAN_MCQ if req.hint["kind"] == "mcq" else CLEAN_ESSAY


def run(design, store, *, providers=None, budget=None, concurrency=4, **kw):
    providers = providers or {p: FakeProvider(p, script=clean_script) for p in {m.provider for m in design.models}}
    return asyncio.run(run_study(design, store, providers, budget=budget or Budget.unlimited(), concurrency=concurrency, **kw)), providers
