from datetime import datetime, timezone

import pytest

from heimdall import pages
from heimdall.config import Config, FocusWindow
from heimdall.schedule import Schedule

from .conftest import EVENING, FOCUS, WORKDAY


@pytest.mark.parametrize("url,site,ptype,item", [
    ("https://www.youtube.com/watch?v=abc123&t=10", "youtube", "watch", "abc123"),
    ("https://m.youtube.com/watch?v=abc123", "youtube", "watch", "abc123"),
    ("https://www.youtube.com/", "youtube", "home", None),
    ("https://www.youtube.com/shorts/xyz", "youtube", "shorts", "xyz"),
    ("https://www.youtube.com/feed/subscriptions", "youtube", "feed", None),
    ("https://www.youtube.com/results?search_query=pytorch", "youtube", "search", None),
    ("https://www.youtube.com/@3blue1brown/videos", "youtube", "channel", None),
    ("https://www.youtube.com/embed/abc123?autoplay=1", "youtube", "embed", "abc123"),
    ("https://www.youtube-nocookie.com/embed/abc123", "youtube", "embed", "abc123"),
    ("https://www.youtube.com/embed/videoseries?list=PL1", "youtube", "embed", None),
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
    s = Schedule(Config(focus_windows=WORKDAY))
    assert s.is_focus(FOCUS)
    assert not s.is_focus(EVENING)
    # Saturday 10:00 PDT
    assert not s.is_focus(datetime(2026, 10, 24, 17, 0, tzinfo=timezone.utc))
    # 16:00 is the end, exclusive; 15:59 is in.
    assert not s.is_focus(datetime(2026, 10, 21, 23, 0, tzinfo=timezone.utc))
    assert s.is_focus(datetime(2026, 10, 21, 22, 59, tzinfo=timezone.utc))


def test_next_change():
    s = Schedule(Config(focus_windows=WORKDAY))
    # Wed 10:00 PDT -> ends Wed 16:00 PDT
    assert s.next_change(FOCUS) == datetime(2026, 10, 21, 23, 0, tzinfo=timezone.utc)
    # Fri 18:00 PDT -> next Mon 09:00 PDT
    fri = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)
    assert s.next_change(fri) == datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)


def test_dst_transition():
    # Mon 2026-11-02 09:30 PST (UTC-8) is focus; the window follows local time.
    s = Schedule(Config(focus_windows=WORKDAY))
    assert s.is_focus(datetime(2026, 11, 2, 17, 30, tzinfo=timezone.utc))
    assert not s.is_focus(datetime(2026, 11, 2, 16, 30, tzinfo=timezone.utc))


def test_default_is_all_day_on_weekdays():
    s = Schedule(Config())
    assert s.is_focus(FOCUS) and s.is_focus(EVENING)
    # Wed 00:00 and 23:59 PDT are in; Sat 00:00 and Sun 23:59 PDT are out.
    assert s.is_focus(datetime(2026, 10, 21, 7, 0, tzinfo=timezone.utc))
    assert s.is_focus(datetime(2026, 10, 22, 6, 59, tzinfo=timezone.utc))
    assert not s.is_focus(datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc))
    assert not s.is_focus(datetime(2026, 10, 26, 6, 59, tzinfo=timezone.utc))
    # Wed -> ends Sat 00:00 PDT; Sat -> starts Mon 00:00 PDT.
    assert s.next_change(FOCUS) == datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)
    sat = datetime(2026, 10, 24, 17, 0, tzinfo=timezone.utc)
    assert s.next_change(sat) == datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("start,end", [("9:00", "16:00"), ("09:00", "24:01"), ("24:00", "24:00"),
                                       ("16:00", "09:00"), ("09:00", "09:00"), ("09:60", "16:00")])
def test_bad_focus_window_rejected(start, end):
    with pytest.raises(ValueError):
        FocusWindow(days=["mon"], start=start, end=end)


def test_force_focus():
    assert Schedule(Config(force_focus=True)).is_focus(EVENING)
    assert not Schedule(Config(force_focus=False)).is_focus(FOCUS)


@pytest.mark.parametrize("url,ptype,item,appealable", [
    ("https://www.reddit.com/", "home", None, False),
    ("https://www.reddit.com/best/", "home", None, False),
    ("https://www.reddit.com/r/popular/", "home", None, False),
    ("https://old.reddit.com/r/all/top/?t=day", "home", None, False),
    ("https://www.reddit.com/r/LocalLLaMA/", "subreddit", None, False),
    ("https://www.reddit.com/r/LocalLLaMA/new/", "subreddit", None, False),
    ("https://www.reddit.com/r/LocalLLaMA/comments/1abcde/some_title/", "post", "1abcde", True),
    ("https://old.reddit.com/r/linux/comments/XyZ12/t/k8s9xyz/", "post", "xyz12", True),
    ("https://www.reddit.com/comments/1abcde", "post", "1abcde", True),
    ("https://www.reddit.com/user/someone/comments/9zz9/hi/", "post", "9zz9", True),
    ("https://www.reddit.com/user/someone/", "user", "u:someone", True),
    ("https://www.reddit.com/search/?q=kv+cache", "search", None, True),
    ("https://www.reddit.com/r/linux/wiki/faq", "other", None, True),
])
def test_parse_reddit(url, ptype, item, appealable):
    p = pages.parse(url)
    assert (p.site, p.page_type, p.item_id) == ("reddit", ptype, item)
    assert pages.appealable(p) is appealable


def test_not_reddit():
    assert pages.parse("https://notreddit.com/r/x/comments/abc/").site == "other"
