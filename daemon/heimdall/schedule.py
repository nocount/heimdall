"""Focus windows: is it focus time right now, and when does that next change?"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import Config, FocusWindow

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _in_window(w: FocusWindow, local: datetime) -> bool:
    if DAYS[local.weekday()] not in w.days:
        return False
    start, end = _parse_hhmm(w.start), _parse_hhmm(w.end)
    return start <= local.time() < end


class Schedule:
    def __init__(self, config: Config):
        self.tz = ZoneInfo(config.timezone)
        self.windows = config.focus_windows
        self.force = config.force_focus

    def local(self, now: datetime) -> datetime:
        return now.astimezone(self.tz)

    def is_focus(self, now: datetime) -> bool:
        if self.force is not None:
            return self.force
        local = self.local(now)
        return any(_in_window(w, local) for w in self.windows)

    def next_change(self, now: datetime) -> datetime | None:
        """The next minute boundary at which is_focus flips, within a week."""
        if self.force is not None:
            return None
        current = self.is_focus(now)
        # Windows are minute-aligned, so stepping by minute is exact. A week is
        # 10k steps of cheap arithmetic, and this only runs on /v1/status.
        t = self.local(now).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(7 * 24 * 60):
            if self.is_focus(t) != current:
                return t
            t += timedelta(minutes=1)
        return None

    def to_dict(self) -> dict:
        """Serializable form the extension caches so it can fail closed offline."""
        return {
            "timezone": str(self.tz),
            "windows": [w.model_dump() for w in self.windows],
            "force_focus": self.force,
        }
