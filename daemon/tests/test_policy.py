from datetime import timedelta

import pytest

from heimdall import pages
from heimdall.config import Config
from heimdall.db import DB, iso
from heimdall.policy import Policy, apply_rules

from .conftest import EVENING, FOCUS, YT_META, FakeClassifier, x_answers, yt_answers

WATCH = "https://www.youtube.com/watch?v=abc123"
TWEET = "https://x.com/someone/status/111"


def make(config, **kw):
    clf = FakeClassifier(**kw)
    return Policy(config, DB(config.db_path), clf), clf


@pytest.mark.parametrize("answers,verdict", [
    (yt_answers(), "allow"),
    (yt_answers(content_type="technical_talk"), "allow"),
    (yt_answers(content_type="commentary_or_opinion"), "block"),
    (yt_answers(edu=0.5), "block"),
    (yt_answers(conf=0.4), "block"),
])
def test_youtube_rules(answers, verdict):
    assert apply_rules("youtube", answers, Config()).verdict == verdict


def test_rule_reason_names_category():
    out = apply_rules("youtube", yt_answers(content_type="entertainment", conf=0.87), Config())
    assert out.reason == "classified as entertainment (87%)"


@pytest.mark.parametrize("ai,verdict", [(0.9, "allow"), (0.7, "allow"), (0.69, "block")])
def test_x_rules(ai, verdict):
    assert apply_rules("x", x_answers(ai=ai), Config()).verdict == verdict


async def test_outside_focus_allows_without_model(config):
    policy, clf = make(config, answers=yt_answers(content_type="gaming"))
    d = await policy.decide(pages.parse(WATCH), YT_META, EVENING)
    assert (d.verdict, d.source) == ("allow", "schedule")
    assert clf.calls == []


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/", "https://www.youtube.com/shorts/x", "https://www.youtube.com/feed/trending",
    "https://x.com/home", "https://x.com/explore", "https://x.com/i/lists/1",
])
async def test_feeds_blocked_by_rule(config, url):
    policy, clf = make(config, answers=yt_answers())
    _, d, served = await policy.evaluate(url, None, FOCUS)
    assert (d.verdict, d.source) == ("block", "rule")
    assert served == "allow"  # shadow mode by default
    assert clf.calls == []


async def test_browse_surfaces_allowed(config):
    policy, _ = make(config)
    for url in ["https://www.youtube.com/results?search_query=x", "https://www.youtube.com/@chan"]:
        _, d, _ = await policy.evaluate(url, None, FOCUS)
        assert (d.verdict, d.source) == ("allow", "rule")


async def test_model_then_cache(config):
    policy, clf = make(config, answers=yt_answers())
    _, d1, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    _, d2, _ = await policy.evaluate(WATCH + "&t=30", YT_META, FOCUS + timedelta(minutes=5))
    assert (d1.source, d1.verdict) == ("model", "allow")
    assert (d2.source, d2.verdict) == ("cache", "allow")
    assert len(clf.calls) == 1
    state, questions = clf.calls[0]
    assert state["video"]["title"] == YT_META["title"]
    assert set(questions) == {"content_type", "educational", "clickbait_mismatch"}


async def test_cache_reapplies_thresholds(config):
    policy, clf = make(config, answers=yt_answers(edu=0.8))
    await policy.evaluate(WATCH, YT_META, FOCUS)
    config.classifier.thresholds.educational = 0.85
    _, d, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    assert (d.source, d.verdict) == ("cache", "block")


async def test_cache_expires(config):
    policy, clf = make(config, answers=x_answers())
    meta = {"author": "@someone", "text": "new attention variant"}
    await policy.evaluate(TWEET, meta, FOCUS)
    await policy.evaluate(TWEET, meta, FOCUS + timedelta(days=1, minutes=1))
    assert len(clf.calls) == 2


async def test_model_error_fails_closed(config):
    policy, _ = make(config, error="timed out")
    _, d, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    assert (d.verdict, d.source, d.reason) == ("block", "error", "classifier unavailable")


async def test_missing_metadata_fails_closed(config):
    policy, clf = make(config, answers=yt_answers())
    _, d, _ = await policy.evaluate(WATCH, {}, FOCUS)
    assert (d.verdict, d.source) == ("block", "error")
    assert clf.calls == []


async def test_enforcing_mode_serves_verdict(config):
    config.shadow = False
    policy, _ = make(config, answers=yt_answers(content_type="gaming"))
    _, d, served = await policy.evaluate(WATCH, YT_META, FOCUS)
    assert d.verdict == served == "block"


async def test_grant_allows(config):
    policy, clf = make(config, answers=yt_answers(content_type="gaming"))
    policy.db.execute("INSERT INTO grants (site, key, expires_at) VALUES (?, ?, ?)",
                      ("youtube", "abc123", iso(FOCUS + timedelta(minutes=20))))
    _, d, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    assert (d.verdict, d.source) == ("allow", "grant")
    _, d, _ = await policy.evaluate(WATCH, YT_META, FOCUS + timedelta(minutes=21))
    assert d.source == "model"


async def test_hard_block_and_always_allow_domains(config):
    config.rules.hard_block_domains = ["reddit.com"]
    config.rules.always_allow_domains = ["youtube.com"]
    policy, _ = make(config)
    _, d, _ = await policy.evaluate("https://old.reddit.com/r/all", None, FOCUS)
    assert d.verdict == "block"
    _, d, _ = await policy.evaluate("https://www.youtube.com/shorts/x", None, FOCUS)
    assert d.verdict == "allow"


async def test_decisions_logged(config):
    policy, _ = make(config, answers=yt_answers(content_type="gaming"))
    decision_id, _, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    row = policy.db.decision(decision_id)
    assert (row["verdict"], row["served"], row["shadow"], row["model"]) == ("block", "allow", 1, "fake:model")
    assert row["reason"].startswith("classified as gaming")
