from datetime import timedelta

import pytest

from heimdall import pages
from heimdall.config import Config, ProviderConfig, Thresholds
from heimdall.db import DB, iso
from heimdall.policy import Policy, apply_rules

from .conftest import EVENING, FOCUS, YT_META, FakeClassifier, yt_answers

WATCH = "https://www.youtube.com/watch?v=abc123"
TWEET = "https://x.com/someone/status/111"


def make(config, **kw):
    clf = FakeClassifier(**kw)
    return Policy(config, DB(config.db_path), clf), clf


TH = Thresholds(worth_learning=0.78)


@pytest.mark.parametrize("worth,verdict", [(0.9, "allow"), (0.78, "allow"), (0.77, "block")])
def test_youtube_rules(worth, verdict):
    assert apply_rules("youtube", yt_answers(worth), TH).verdict == verdict


def test_rule_reason():
    assert apply_rules("youtube", yt_answers(0.12), TH).reason == "rated only 12% worth learning from"


def test_fallback_answers_use_fallback_thresholds():
    c = Config().classifier
    c.fallback = ProviderConfig(provider="workers_ai", model="@cf/cloudflare/clef-flash",
                                thresholds=Thresholds(worth_learning=0.7))
    assert c.thresholds_for("workers_ai:@cf/cloudflare/clef-flash").worth_learning == 0.7
    assert c.thresholds_for("openrouter:typesafe/jev-1.13").worth_learning == 0.78
    assert c.thresholds_for(None).worth_learning == 0.78


async def test_outside_focus_allows_without_model(config):
    policy, clf = make(config, answers=yt_answers(worth=0.2))
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
    assert set(questions) == {"worth_learning"}


async def test_cache_reapplies_thresholds(config):
    policy, clf = make(config, answers=yt_answers(0.84))
    await policy.evaluate(WATCH, YT_META, FOCUS)
    config.classifier.thresholds.worth_learning = 0.85
    _, d, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    assert (d.source, d.verdict) == ("cache", "block")


async def test_stale_cache_from_old_questions_is_a_miss(config):
    policy, clf = make(config, answers=yt_answers(0.9))
    policy.db.cache_answers("youtube", "abc123", "fake:model", {"educational": {"noul": 0.1}},
                            FOCUS + timedelta(days=1))
    _, d, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    assert (d.source, d.verdict) == ("model", "allow")
    _, d, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    assert d.source == "cache"  # replaced with current answers


async def test_allowlisted_channel_skips_model(config):
    config.rules.youtube.allow_channels = ["UCuniversity"]
    policy, clf = make(config, answers=yt_answers(0.1))
    _, d, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    assert (d.verdict, d.source, d.reason) == ("allow", "channel", "allowlisted channel: Some University")
    assert clf.calls == []
    # Other channels still go to the model, and Shorts stay blocked even from allowlisted channels.
    _, d, _ = await policy.evaluate(WATCH, {**YT_META, "channel_id": "UCother"}, FOCUS)
    assert (d.verdict, d.source) == ("block", "model")
    _, d, _ = await policy.evaluate("https://www.youtube.com/shorts/x", YT_META, FOCUS)
    assert (d.verdict, d.source) == ("block", "rule")


async def test_cache_expires(config):
    policy, clf = make(config, answers=yt_answers())
    await policy.evaluate(WATCH, YT_META, FOCUS)
    await policy.evaluate(WATCH, YT_META, FOCUS + timedelta(days=30, minutes=1))
    assert len(clf.calls) == 2


@pytest.mark.parametrize("url", [
    TWEET, "https://x.com/karpathy", "https://x.com/search?q=llm", "https://twitter.com/home",
    "https://x.com/messages", "https://mobile.x.com/someone/status/222",
])
async def test_x_blocked_without_model(config, url):
    policy, clf = make(config, answers=yt_answers())
    _, d, _ = await policy.evaluate(url, {"author": "@someone", "text": "attention is all you need"}, FOCUS)
    assert (d.verdict, d.source) == ("block", "rule")
    assert clf.calls == []


async def test_x_grant_opens_one_post(config):
    policy, _ = make(config)
    policy.db.execute("INSERT INTO grants (site, key, expires_at) VALUES (?, ?, ?)",
                      ("x", "111", iso(FOCUS + timedelta(minutes=10))))
    _, d, _ = await policy.evaluate(TWEET, None, FOCUS)
    assert (d.verdict, d.source) == ("allow", "grant")
    _, d, _ = await policy.evaluate("https://x.com/someone/status/999", None, FOCUS)
    assert d.verdict == "block"


async def test_x_allowed_if_removed_from_hard_block(config):
    config.rules.hard_block_domains = []
    policy, clf = make(config)
    _, d, _ = await policy.evaluate(TWEET, None, FOCUS)
    assert (d.verdict, d.reason) == ("allow", "not a filtered site")
    assert clf.calls == []


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
    policy, _ = make(config, answers=yt_answers(worth=0.2))
    _, d, served = await policy.evaluate(WATCH, YT_META, FOCUS)
    assert d.verdict == served == "block"


async def test_grant_allows(config):
    policy, clf = make(config, answers=yt_answers(worth=0.2))
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
    policy, _ = make(config, answers=yt_answers(worth=0.2))
    decision_id, _, _ = await policy.evaluate(WATCH, YT_META, FOCUS)
    row = policy.db.decision(decision_id)
    assert (row["verdict"], row["served"], row["shadow"], row["model"]) == ("block", "allow", 1, "fake:model")
    assert row["reason"] == "rated only 20% worth learning from"
