"""Client for the System One decision API (`/v1/systemone`), across three hosts.

All three speak the same request ({model, state, questions}) and answer shapes:
  noul:   {"type": "noul", "noul": 0.96}
  choice: {"type": "choice", "choice": "...", "confidence": 0.91, "probabilities": {...}}
  score:  {"type": "score", "score": 1.1, "confidence": ..., "probabilities": {...}, "legend": {...}}
Workers AI wraps the body in Cloudflare's {"result": ..., "success": ...} envelope.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import httpx

from ..config import ProviderConfig

WORKERS_AI_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
OPENROUTER_URL = "https://openrouter.ai/api/v1/systemone"


class SystemOneError(Exception):
    pass


@dataclass
class Result:
    answers: dict
    model: str  # "<provider>:<model>", recorded with every decision
    latency_ms: int
    usage: dict = field(default_factory=dict)


class Provider:
    def __init__(self, cfg: ProviderConfig, http: httpx.AsyncClient, env: dict | None = None):
        self.cfg = cfg
        self.http = http
        self.env = os.environ if env is None else env
        self.timeout = cfg.timeout_ms / 1000

    @property
    def label(self) -> str:
        return self.cfg.label

    def _secret(self, name: str) -> str:
        value = self.env.get(name)
        if not value:
            raise SystemOneError(f"{name} is not set")
        return value

    def endpoint(self) -> tuple[str, dict, str]:
        """(url, headers, model name for the request body)."""
        raise NotImplementedError

    def unwrap(self, body: dict) -> dict:
        return body

    async def ask(self, state, questions: dict) -> Result:
        url, headers, model = self.endpoint()
        payload = {"model": model, "state": state, "questions": questions}
        start = time.perf_counter()
        try:
            resp = await self.http.post(url, json=payload, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise SystemOneError(f"{self.label} timed out after {self.cfg.timeout_ms}ms") from e
        except httpx.HTTPError as e:
            raise SystemOneError(f"{self.label} request failed: {e}") from e
        latency_ms = round((time.perf_counter() - start) * 1000)
        if resp.status_code != 200:
            raise SystemOneError(f"{self.label} HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            body = self.unwrap(resp.json())
            answers = body["answers"]
        except (ValueError, KeyError, TypeError) as e:
            raise SystemOneError(f"{self.label} malformed response: {resp.text[:300]}") from e
        missing = set(questions) - set(answers)
        if missing:
            raise SystemOneError(f"{self.label} did not answer {sorted(missing)}")
        return Result(answers, self.label, latency_ms, body.get("usage") or {})


class WorkersAI(Provider):
    def endpoint(self):
        account = self._secret("CF_ACCOUNT_ID")
        token = self._secret("CF_API_TOKEN")
        # Body wants the bare name: "@cf/cloudflare/clef-flash" -> "clef-flash".
        url = self.cfg.url or WORKERS_AI_URL.format(account=account, model=self.cfg.model)
        return url, {"Authorization": f"Bearer {token}"}, self.cfg.model.rsplit("/", 1)[-1]

    def unwrap(self, body: dict) -> dict:
        if not body.get("success", True):
            raise SystemOneError(f"{self.label} error: {body.get('errors')}")
        return body["result"]


class OpenRouter(Provider):
    def endpoint(self):
        key = self._secret("OPENROUTER_API_KEY")
        return self.cfg.url or OPENROUTER_URL, {"Authorization": f"Bearer {key}"}, self.cfg.model


class LlamaCpp(Provider):
    def endpoint(self):
        if not self.cfg.url:
            raise SystemOneError("local_llamacpp needs a url")
        return self.cfg.url.rstrip("/") + "/v1/systemone", {}, self.cfg.model


PROVIDERS = {"workers_ai": WorkersAI, "openrouter": OpenRouter, "local_llamacpp": LlamaCpp}


def make_provider(cfg: ProviderConfig, http: httpx.AsyncClient, env: dict | None = None) -> Provider:
    return PROVIDERS[cfg.provider](cfg, http, env)


class Classifier:
    """Primary provider with an optional fallback. Raises SystemOneError if both fail."""

    def __init__(self, primary: Provider, fallback: Provider | None = None):
        self.primary = primary
        self.fallback = fallback

    async def ask(self, state, questions: dict) -> Result:
        try:
            return await self.primary.ask(state, questions)
        except SystemOneError as primary_err:
            if self.fallback is None:
                raise
            try:
                return await self.fallback.ask(state, questions)
            except SystemOneError as fallback_err:
                raise SystemOneError(f"{primary_err}; fallback: {fallback_err}") from fallback_err
