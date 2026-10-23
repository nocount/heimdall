"""The policy pipeline (PLAN.md section 6). First match wins:

1. outside focus window      -> allow
2. active grant for the page -> allow
3. static rules              -> block feeds/shorts/timelines, allow browse surfaces
4. cached model answers      -> re-apply rules
5. model call                -> apply rules
6. model error / no metadata -> block (fail closed)

Every evaluation is logged. In shadow mode the extension is always told
"allow" and the would-be verdict is logged.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from datetime import datetime

from . import pages
from .cache import AnswerCache
from .config import Config, Thresholds
from .db import DB, iso
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


@dataclass
class RuleOutcome:
    verdict: str
    reason: str
    category: str | None
    details: dict = field(default_factory=dict)


def _pct(p: float) -> str:
    return f"{round(p * 100)}%"


def apply_rules(site: str, answers: dict, th: Thresholds) -> RuleOutcome:
    """Combine atomic answers into a verdict. Pure; the bench uses this too.

    YouTube is judged on `educational` alone. Phase 0 showed content_type
    gating blocked the AI interviews and podcasts that should be allowed (the
    models rightly call them commentary), so content_type only names the
    category in the reason.
    """
    ctype = answers.get("content_type") or {}
    choice = ctype.get("choice")
    choice_p = (ctype.get("probabilities") or {}).get(choice, ctype.get("confidence", 0.0))
    category = f"{choice} ({_pct(choice_p)})" if choice else None

    if site == "youtube":
        edu = answers["educational"]["noul"]
        details = {"educational": edu, "content_type": choice}
        if edu >= th.educational:
            return RuleOutcome(ALLOW, f"{_pct(edu)} educational ({category})", category, details)
        return RuleOutcome(BLOCK, f"only {_pct(edu)} educational ({category})", category, details)

    if site == "x":
        ai = answers["ai_related"]["noul"]
        details = {"ai_related": ai, "content_type": choice}
        if ai >= th.ai_related:
            return RuleOutcome(ALLOW, f"{_pct(ai)} AI-related ({category})", category, details)
        return RuleOutcome(BLOCK, f"only {_pct(ai)} AI-related ({category})", category, details)

    raise ValueError(f"no rules for site {site}")


def static_rule(page: Page, config: Config) -> tuple[str, str] | None:
    """Rules that need no model call. Returns (verdict, reason) or None to go to the model."""
    rules = config.rules
    if pages.host_matches(page.host, rules.always_allow_domains):
        return ALLOW, "always-allowed domain"
    if pages.host_matches(page.host, rules.hard_block_domains):
        return BLOCK, f"{page.host} is blocked during focus hours"
    if page.site == "other":
        return ALLOW, "not a filtered site"

    site_rules = getattr(rules, page.site)
    for pattern in site_rules.block_paths:
        if fnmatch.fnmatchcase(page.path, pattern):
            return BLOCK, f"{page.site} {page.page_type} pages are blocked during focus hours"
    if page.page_type in pages.MODEL_PAGE_TYPES.get(page.site, set()):
        return None
    return ALLOW, f"{page.site} {page.page_type} pages are allowed"


class Policy:
    def __init__(self, config: Config, db: DB, classifier: Classifier):
        self.config = config
        self.db = db
        self.schedule = Schedule(config)
        self.cache = AnswerCache(db, config.cache_ttl_days)
        self.classifier = classifier

    def _grant_key(self, page: Page) -> str:
        return page.item_id or page.url

    async def decide(self, page: Page, metadata: dict | None, now: datetime) -> Decision:
        if not self.schedule.is_focus(now):
            return Decision(ALLOW, "schedule", "outside focus hours")

        if self.db.active_grant(page.site, self._grant_key(page), now):
            return Decision(ALLOW, "grant", "granted on appeal")

        if (static := static_rule(page, self.config)) is not None:
            verdict, reason = static
            return Decision(verdict, "rule", reason, ttl_s=300)

        ttl_s = 300 if page.site == "x" else 3600

        if (hit := self.cache.get(page, now)) is not None:
            model, answers = hit
            out = apply_rules(page.site, answers, self.config.classifier.thresholds_for(model))
            return Decision(out.verdict, "cache", out.reason, out.category, answers, model, 0, ttl_s)

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
