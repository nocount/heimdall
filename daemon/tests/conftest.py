from datetime import datetime, timezone

import pytest

from heimdall.config import Config
from heimdall.models.systemone import Result, SystemOneError

# Wednesday 2026-10-21 10:00 PDT, inside the default focus window.
FOCUS = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)
# Wednesday 2026-10-21 20:00 PDT.
EVENING = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)


def yt_answers(worth=0.9):
    return {"worth_learning": {"type": "noul", "noul": worth}}


class FakeClassifier:
    """Stands in for systemone.Classifier: returns canned answers, records calls."""

    def __init__(self, answers=None, error=None):
        self.answers = answers
        self.error = error
        self.calls = []

        class _P:
            label = "fake:model"

        self.primary, self.fallback = _P(), None

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise SystemOneError(self.error)
        return Result(self.answers, "fake:model", 42)


@pytest.fixture
def config(tmp_path):
    return Config(db_path=str(tmp_path / "test.db"), shadow=True)


YT_META = {
    "channel_id": "UCuniversity",
    "title": "Building a Transformer from Scratch — Lecture 4",
    "channel": "Some University",
    "category": "Education",
    "tags": ["machine learning", "pytorch"],
    "description": "In this lecture we implement attention.",
}
