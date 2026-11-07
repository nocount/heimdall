from datetime import datetime, timezone

import pytest

from heimdall.config import Config, FocusWindow
from heimdall.models.systemone import Result, SystemOneError

# Most tests use a 09:00-16:00 weekday window (the default is all day on
# weekdays), so that EVENING is outside focus hours.
WORKDAY = [FocusWindow(days=["mon", "tue", "wed", "thu", "fri"], start="09:00", end="16:00")]

# Wednesday 2026-10-21 10:00 PDT, inside the focus window.
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
    # YouTube judged by the classifier (the default config blocks it outright),
    # so these tests keep exercising that path.
    c = Config(db_path=str(tmp_path / "test.db"), shadow=True, focus_windows=WORKDAY)
    c.rules.hard_block_domains = ["x.com", "twitter.com"]
    return c


YT_META = {
    "channel_id": "UCuniversity",
    "title": "Building a Transformer from Scratch — Lecture 4",
    "channel": "Some University",
    "category": "Education",
    "tags": ["machine learning", "pytorch"],
    "description": "In this lecture we implement attention.",
}
