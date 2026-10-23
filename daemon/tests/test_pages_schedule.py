from datetime import datetime, timezone

import pytest

from heimdall import pages
from heimdall.config import Config
from heimdall.schedule import Schedule

from .conftest import EVENING, FOCUS


@pytest.mark.parametrize("url,site,ptype,item", [
    ("https://www.youtube.com/watch?v=abc123&t=10", "youtube", "watch", "abc123"),
    ("https://m.youtube.com/watch?v=abc123", "youtube", "watch", "abc123"),
    ("https://www.youtube.com/", "youtube", "home", None),
    ("https://www.youtube.com/shorts/xyz", "youtube", "shorts", "xyz"),
    ("https://www.youtube.com/feed/subscriptions", "youtube", "feed", None),
    ("https://www.youtube.com/results?search_query=pytorch", "youtube", "search", None),
    ("https://www.youtube.com/@3blue1brown/videos", "youtube", "channel", None),
    ("https://x.com/karpathy/status/1234567890", "x", "status", "1234567890"),
    ("https://twitter.com/karpathy/status/1234567890/photo/1", "x", "status", "1234567890"),
    ("https://x.com/home", "x", "home", None),
    ("https://x.com/explore/tabs/trending", "x", "explore", None),
    ("https://x.com/notifications", "x", "notifications", None),
    ("https://x.com/i/lists/42", "x", "list", None),
    ("https://x.com/Karpathy", "x", "profile", "@karpathy"),
    ("https://x.com/karpathy/with_replies", "x", "profile", "@karpathy"),
    ("https://x.com/search?q=LLM%20evals&src=typed", "x", "search", "q:llm evals"),
    ("https://x.com/messages", "x", "other", None),
    ("https://x.com/settings/account", "x", "other", None),
    ("https://example.com/watch?v=1", "other", "other", None),
])
def test_parse(url, site, ptype, item):
    p = pages.parse(url)
    assert (p.site, p.page_type, p.item_id) == (site, ptype, item)


def test_host_matches():
    assert pages.host_matches("old.reddit.com", ["reddit.com"])
    assert pages.host_matches("reddit.com", ["reddit.com"])
    assert not pages.host_matches("notreddit.com", ["reddit.com"])


def test_focus_window():
    s = Schedule(Config())
    assert s.is_focus(FOCUS)
    assert not s.is_focus(EVENING)
    # Saturday 10:00 PDT
    assert not s.is_focus(datetime(2026, 10, 24, 17, 0, tzinfo=timezone.utc))
    # 17:00 is the end, exclusive; 16:59 is in.
    assert not s.is_focus(datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc))
    assert s.is_focus(datetime(2026, 10, 21, 23, 59, tzinfo=timezone.utc))


def test_next_change():
    s = Schedule(Config())
    # Wed 10:00 PDT -> ends Wed 17:00 PDT
    assert s.next_change(FOCUS) == datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
    # Fri 18:00 PDT -> next Mon 09:00 PDT
    fri = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)
    assert s.next_change(fri) == datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)


def test_dst_transition():
    # Mon 2026-11-02 09:30 PST (UTC-8) is focus; the window follows local time.
    s = Schedule(Config())
    assert s.is_focus(datetime(2026, 11, 2, 17, 30, tzinfo=timezone.utc))
    assert not s.is_focus(datetime(2026, 11, 2, 16, 30, tzinfo=timezone.utc))


def test_force_focus():
    assert Schedule(Config(force_focus=True)).is_focus(EVENING)
    assert not Schedule(Config(force_focus=False)).is_focus(FOCUS)
