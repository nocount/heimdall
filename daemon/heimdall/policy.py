"""The policy pipeline (PLAN.md section 6). First match wins:

1. outside focus window      -> allow
2. active grant for the page -> allow
3. static rules              -> block X, feeds and shorts; allow browse surfaces
   allowlisted channel       -> allow, no model call
4. cached model answers      -> re-apply rules
5. model call                -> apply rules
6. model error / no metadata -> block (fail closed)

Every evaluation is logged. In shadow mode the extension is always told
"allow" and the would-be verdict is logged.

Blocked pages can be appealed (except feeds and Shorts): the appeal judge
reads a one-sentence justification, and a grant opens that one page for 10-40
minutes. The limits around it (length, daily cap, cooldown, max grant) are
enforced here, not by the model.
"""

from __future__ import annotations

import fnmatch
import logging
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import pages
from .cache import AnswerCache
from .config import Config, Thresholds
from .db import DB, iso
from .models.appeals import AppealCase, AppealJudge, AppealJudgeError
from .models.systemone import Classifier, SystemOneError
from .pages import Page
from .questions import build_request
from .schedule import Schedule

log = logging.getLogger("heimdall.policy")

ALLOW, BLOCK = "allow", "block"


@dataclass
class Decision:
    verdict: str
    source: str
    reason: str
    category: str | None = None
    answers: dict | None = None
    model: str | None = None
    latency_ms: int | None = None
    ttl_s: int = 0  # how long the extension may trust this verdict for the tab
    expires_at: str | None = None  # for grants: when the extension must re-check the tab


@dataclass
class RuleOutcome:
    verdict: str
    reason: str
    category: str | None
    details: dict = field(default_factory=dict)


def _pct(p: float) -> str:
    return f"{round(p * 100)}%"


def apply_rules(site: str, answers: dict, th: Thresholds) -> RuleOutcome:
    """Turn the model's answers into a verdict. Pure; the bench uses this too."""
    if site == "youtube":
        p = answers["worth_learning"]["noul"]
        details = {"worth_learning": p}
        if p >= th.worth_learning:
            return RuleOutcome(ALLOW, f"rated {_pct(p)} worth learning from", None, details)
        return RuleOutcome(BLOCK, f"rated only {_pct(p)} worth learning from", None, details)

    raise ValueError(f"no rules for site {site}")


def allowlisted_channel(page: Page, metadata: dict | None, config: Config) -> bool:
    channel_id = (metadata or {}).get("channel_id")
    return page.site == "youtube" and bool(channel_id) and channel_id in config.rules.youtube.allow_channels


def static_rule(page: Page, config: Config) -> tuple[str, str] | None:
    """Rules that need no model call. Returns (verdict, reason) or None to go to the model."""
    rules = config.rules
    if pages.host_matches(page.host, rules.always_allow_domains):
        return ALLOW, "always-allowed domain"
    if pages.host_matches(page.host, rules.hard_block_domains):
        return BLOCK, f"{page.host} is blocked during focus hours"
    if page.site not in pages.MODEL_PAGE_TYPES:
        return ALLOW, "not a filtered site"

    site_rules = getattr(rules, page.site)
    for pattern in site_rules.block_paths:
        if fnmatch.fnmatchcase(page.path, pattern):
            return BLOCK, f"{page.site} {page.page_type} pages are blocked during focus hours"
    if page.page_type in pages.MODEL_PAGE_TYPES.get(page.site, set()):
        return None
    return ALLOW, f"{page.site} {page.page_type} pages are allowed"


class AppealRejected(Exception):
    """An appeal refused before or instead of judging. `status` maps to HTTP."""

    def __init__(self, status: int, detail: str, retry_after_s: int | None = None):
        super().__init__(detail)
        self.status, self.detail, self.retry_after_s = status, detail, retry_after_s


class Policy:
    def __init__(self, config: Config, db: DB, classifier: Classifier, judge: AppealJudge | None = None):
        self.config = config
        self.db = db
        self.schedule = Schedule(config)
        self.cache = AnswerCache(db, config.cache_ttl_days)
        self.classifier = classifier
        self.judge = judge

    def _grant_key(self, page: Page) -> str:
        return page.item_id or page.url

    async def decide(self, page: Page, metadata: dict | None, now: datetime) -> Decision:
        if not self.schedule.is_focus(now):
            return Decision(ALLOW, "schedule", "outside focus hours")

        if expires := self.db.active_grant(page.site, self._grant_key(page), now):
            return Decision(ALLOW, "grant", "granted on appeal", expires_at=expires)

        if (static := static_rule(page, self.config)) is not None:
            verdict, reason = static
            return Decision(verdict, "rule", reason, ttl_s=300)

        ttl_s = 3600

        if allowlisted_channel(page, metadata, self.config):
            return Decision(ALLOW, "channel", f"allowlisted channel: {metadata.get('channel')}", ttl_s=ttl_s)

        if (hit := self.cache.get(page, now)) is not None:
            model, answers = hit
            try:
                out = apply_rules(page.site, answers, self.config.classifier.thresholds_for(model))
                return Decision(out.verdict, "cache", out.reason, out.category, answers, model, 0, ttl_s)
            except (KeyError, TypeError):
                pass  # cached under an older question set: ask again, and the put below replaces it

        try:
            state, questions = build_request(page, metadata or {})
        except ValueError as e:
            log.warning("metadata extraction failed for %s: %s", page.url, e)
            return Decision(BLOCK, "error", f"couldn't read the page ({e})")

        try:
            result = await self.classifier.ask(state, questions)
        except SystemOneError as e:
            log.error("classifier unavailable: %s", e)
            return Decision(BLOCK, "error", "classifier unavailable")

        try:
            out = apply_rules(page.site, result.answers, self.config.classifier.thresholds_for(result.model))
        except (KeyError, TypeError) as e:
            log.error("unexpected answer shape from %s: %s", result.model, result.answers)
            return Decision(BLOCK, "error", f"classifier returned an unexpected answer ({e})",
                            answers=result.answers, model=result.model, latency_ms=result.latency_ms)
        self.cache.put(page, result.model, result.answers, now)
        return Decision(out.verdict, "model", out.reason, out.category, result.answers,
                        result.model, result.latency_ms, ttl_s)

    async def evaluate(self, url: str, metadata: dict | None, now: datetime) -> tuple[int, Decision, str]:
        """Decide, log, and return (decision_id, decision, served verdict)."""
        page = pages.parse(url)
        focus = self.schedule.is_focus(now)
        decision = await self.decide(page, metadata, now)
        served = ALLOW if self.config.shadow else decision.verdict
        decision_id = self.db.log_decision(
            ts=iso(now), url=url, site=page.site, page_type=page.page_type, item_id=page.item_id,
            metadata=metadata or None, source=decision.source, model=decision.model,
            probs=decision.answers, verdict=decision.verdict, served=served,
            shadow=int(self.config.shadow), focus=int(focus), latency_ms=decision.latency_ms,
            reason=decision.reason, category=decision.category,
        )
        log.info("#%d %s %s/%s -> %s%s (%s: %s)", decision_id, url, page.site, page.page_type,
                 decision.verdict, " [shadow]" if self.config.shadow else "", decision.source,
                 decision.reason)
        return decision_id, decision, served

    # appeals

    def _day_start(self, now: datetime) -> datetime:
        return self.schedule.local(now).replace(hour=0, minute=0, second=0, microsecond=0)

    def appeals_left(self, now: datetime) -> int:
        used = len(self.db.appeals_since(self._day_start(now)))
        return max(0, self.config.appeals.max_per_day - used)

    async def appeal(self, decision_id: int, justification: str, now: datetime) -> dict:
        cfg = self.config.appeals
        row = self.db.decision(decision_id)
        if row is None:
            raise AppealRejected(404, "unknown decision")
        page = pages.parse(row["url"])
        if row["verdict"] != BLOCK:
            raise AppealRejected(409, "that page wasn't blocked")
        if not pages.appealable(page):
            raise AppealRejected(403, "feeds and Shorts can't be appealed")
        if not self.schedule.is_focus(now):
            raise AppealRejected(409, "focus hours are over; just reload the page")

        justification = " ".join(justification.split())
        if len(justification.split()) < cfg.min_words:
            raise AppealRejected(422, f"say why in at least {cfg.min_words} words")

        today = self.db.appeals_since(self._day_start(now))
        if len(today) >= cfg.max_per_day:
            raise AppealRejected(429, f"no appeals left today ({cfg.max_per_day} used)")

        key = self._grant_key(page)
        if denied_at := self.db.last_denial(page.site, key):
            until = datetime.fromisoformat(denied_at) + timedelta(minutes=cfg.deny_cooldown_min)
            if until > now:
                wait = int((until - now).total_seconds()) + 1
                raise AppealRejected(429, f"appeal denied recently; try again in {wait // 60 + 1} min", wait)

        if self.judge is None:
            raise AppealRejected(503, "appeals aren't configured")
        case = AppealCase(
            url=row["url"], page=f"{page.site} {page.page_type}",
            metadata=json.loads(row["metadata"]) if row["metadata"] else None,
            block_reason=row["reason"] or "", justification=justification,
            today=[{"justification": a["justification"], "granted": bool(a["granted"]), "url": a["url"]}
                   for a in today],
        )
        try:
            verdict, record = await self.judge.judge(case)
        except AppealJudgeError as e:
            # Not counted against the daily cap: the judge, not the user, failed.
            log.error("%s", e)
            raise AppealRejected(503, "the appeal judge is unavailable right now") from e

        minutes = min(verdict.minutes, cfg.max_grant_min) if verdict.grant else None
        appeal_id = self.db.add_appeal(ts=now, decision_id=decision_id, justification=justification,
                                       model_output=record, granted=verdict.grant, minutes=minutes,
                                       site=page.site, key=key)
        expires_at = None
        if verdict.grant:
            expires = now + timedelta(minutes=minutes)
            self.db.add_grant(page.site, key, expires, appeal_id)
            expires_at = iso(expires)
        log.info("appeal #%d on decision #%d %s: %s (%s)", appeal_id, decision_id,
                 "GRANTED %d min" % minutes if verdict.grant else "denied", justification, verdict.message)
        return {
            "granted": verdict.grant,
            "minutes": minutes,
            "message": verdict.message,
            "expires_at": expires_at,
            "url": row["url"],
            "appeals_left": max(0, cfg.max_per_day - len(today) - 1),
            "cooldown_s": None if verdict.grant else cfg.deny_cooldown_min * 60,
        }
