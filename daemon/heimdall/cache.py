"""Model-answer cache keyed by video ID / tweet ID.

It stores the model's raw answers, not the verdict, so retuned thresholds take
effect immediately on cached pages.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .db import DB
from .pages import Page


class AnswerCache:
    def __init__(self, db: DB, ttl_days: dict[str, float]):
        self.db = db
        self.ttl_days = ttl_days

    def get(self, page: Page, now: datetime) -> tuple[str, dict] | None:
        if not page.item_id:
            return None
        return self.db.cached_answers(page.site, page.item_id, now)

    def put(self, page: Page, model: str, answers: dict, now: datetime) -> None:
        ttl = self.ttl_days.get(page.site, 0)
        if not page.item_id or ttl <= 0:
            return
        self.db.cache_answers(page.site, page.item_id, model, answers, now + timedelta(days=ttl))
