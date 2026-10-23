"""Config loading. The YAML file is the single source of truth for tunables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = "/etc/heimdall/config.yaml"

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ProviderName = Literal["workers_ai", "openrouter", "local_llamacpp"]


class FocusWindow(BaseModel):
    days: list[Day]
    start: str  # "HH:MM"
    end: str


class Thresholds(BaseModel):
    worth_learning: float = 0.78


class ProviderConfig(BaseModel):
    provider: ProviderName
    model: str
    url: str | None = None  # base URL override; required for local_llamacpp
    timeout_ms: int = 1500
    # Models aren't calibrated alike, so a fallback needs its own thresholds.
    thresholds: Thresholds | None = None

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


class ClassifierConfig(ProviderConfig):
    thresholds: Thresholds = Thresholds()
    fallback: ProviderConfig | None = None

    def thresholds_for(self, model_label: str | None) -> Thresholds:
        """Thresholds for whichever provider produced the answers."""
        fb = self.fallback
        if fb is not None and fb.thresholds is not None and model_label == fb.label:
            return fb.thresholds
        return self.thresholds


DEFAULT_FOCUS_RULES = """\
During focus hours I want to work. On YouTube I only watch videos that help me
learn about AI, technology or science: lectures, talks, expert interviews and
in-depth analysis, not entertainment, gaming, reactions or hype. X is blocked
outright because its feeds are a time sink; I open a specific post or profile
only when I have a concrete reason."""


class AppealsConfig(BaseModel):
    model: str = "claude-haiku-5-5"
    min_words: int = 8
    max_per_day: int = 5
    deny_cooldown_min: int = 10
    max_grant_min: int = 40
    timeout_s: float = 20
    # What the judge is told you want from focus hours, in your own words.
    focus_rules: str = DEFAULT_FOCUS_RULES


class SiteRules(BaseModel):
    block_paths: list[str] = []
    # YouTube channel IDs (UC...) whose videos are allowed without a model call.
    allow_channels: list[str] = []


class Rules(BaseModel):
    youtube: SiteRules = SiteRules(block_paths=["/", "/shorts/*", "/feed/*"])
    # X is blocked outright; only an appeal grant opens a specific post or profile.
    hard_block_domains: list[str] = ["x.com", "twitter.com"]
    always_allow_domains: list[str] = []


class WatchdogConfig(BaseModel):
    heartbeat_timeout_s: int = 60
    startup_grace_s: int = 90
    kill_browsers: list[str] = []


class Config(BaseModel):
    timezone: str = "America/Los_Angeles"
    focus_windows: list[FocusWindow] = [
        FocusWindow(days=["mon", "tue", "wed", "thu", "fri"], start="09:00", end="17:00")
    ]
    shadow: bool = False
    force_focus: bool | None = None
    db_path: str = "/var/lib/heimdall/heimdall.db"
    classifier: ClassifierConfig = ClassifierConfig(provider="openrouter", model="typesafe/jev-1.13")
    appeals: AppealsConfig = AppealsConfig()
    rules: Rules = Rules()
    cache_ttl_days: dict[str, float] = Field(default_factory=lambda: {"youtube": 30})
    watchdog: WatchdogConfig = WatchdogConfig()


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config from `path`, $HEIMDALL_CONFIG, or the system default.

    $HEIMDALL_DB overrides db_path so a dev run never touches /var/lib.
    """
    path = Path(path or os.environ.get("HEIMDALL_CONFIG") or DEFAULT_CONFIG_PATH)
    data = yaml.safe_load(path.read_text()) or {}
    config = Config.model_validate(data)
    if db := os.environ.get("HEIMDALL_DB"):
        config.db_path = db
    return config
