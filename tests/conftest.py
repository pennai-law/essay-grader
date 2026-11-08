from pathlib import Path

import pytest

from essay_grader.rubric import load_rubric

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fix():
    return FIX


@pytest.fixture
def draft():
    return load_rubric(FIX / "rubric.yaml")


@pytest.fixture
def locked(draft):
    return draft.lock()


@pytest.fixture
def exam_text():
    return (FIX / "exam.txt").read_text()
