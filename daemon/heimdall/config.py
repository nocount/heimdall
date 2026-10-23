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
    educational: float = 0.81
    ai_related: float = 0.7


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


class AppealsConfig(BaseModel):
    model: str = "claude-haiku-5-5"
    min_words: int = 8
    max_per_day: int = 5
    deny_cooldown_min: int = 10
    max_grant_min: int = 40


class SiteRules(BaseModel):
    block_paths: list[str] = []


class Rules(BaseModel):
    youtube: SiteRules = SiteRules(block_paths=["/", "/shorts/*", "/feed/*"])
    x: SiteRules = SiteRules(block_paths=["/home", "/explore", "/notifications", "/i/lists/*"])
    hard_block_domains: list[str] = []
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
    shadow: bool = True
    force_focus: bool | None = None
    db_path: str = "/var/lib/heimdall/heimdall.db"
    classifier: ClassifierConfig = ClassifierConfig(provider="openrouter", model="typesafe/jev-1.13")
    appeals: AppealsConfig = AppealsConfig()
    rules: Rules = Rules()
    cache_ttl_days: dict[str, float] = Field(default_factory=lambda: {"youtube": 30, "x": 1})
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
