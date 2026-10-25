"""FastAPI app. Bound to 127.0.0.1 only; see `heimdall serve`."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__
from .config import Config, load_config
from .db import DB, iso
from .models.appeals import AppealJudge
from .models.systemone import Classifier, make_provider
from .pages import appealable, parse
from .policy import AppealRejected, Policy

log = logging.getLogger("heimdall")


class ClassifyRequest(BaseModel):
    url: str
    metadata: dict | None = None


class AppealRequest(BaseModel):
    decision_id: int
    justification: str


class HeartbeatRequest(BaseModel):
    client: str = "firefox"
    version: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_judge(config: Config) -> AppealJudge | None:
    """The appeal judge, or None (appeals refused, block page says so) if it can't work."""
    try:
        judge = AppealJudge(config.appeals)
    except Exception as e:
        log.warning("appeals disabled: %s", e)
        return None
    if not judge.configured:
        log.warning("appeals disabled: no Anthropic credentials (set ANTHROPIC_API_KEY in secrets.env)")
        return None
    return judge


def create_app(config: Config | None = None, classifier: Classifier | None = None,
               clock=utcnow, judge: AppealJudge | None = None) -> FastAPI:
    config = config or load_config()
    db = DB(config.db_path)
    http = httpx.AsyncClient()
    if classifier is None:
        fallback = config.classifier.fallback
        classifier = Classifier(
            make_provider(config.classifier, http),
            make_provider(fallback, http) if fallback else None,
        )
    policy = Policy(config, db, classifier, judge if judge is not None else make_judge(config))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await http.aclose()

    app = FastAPI(title="heimdall", version=__version__, lifespan=lifespan)
    app.state.config, app.state.db, app.state.policy = config, db, policy

    # Firefox preflights the extension's JSON POSTs to loopback despite its
    # host permission, so answer CORS, but only for extension origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"moz-extension://[0-9a-f-]+",
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def only_extension_origins(request: Request, call_next):
        # Any web page can fire requests at 127.0.0.1. Browsers always attach
        # an Origin to cross-origin fetches, so refuse anything that isn't
        # from an extension. No Origin means curl or the CLI.
        origin = request.headers.get("origin")
        if origin is not None and not origin.startswith("moz-extension://"):
            log.warning("refused %s %s from origin %r", request.method, request.url.path, origin)
            return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        return await call_next(request)

    @app.post("/v1/classify")
    async def classify(req: ClassifyRequest):
        decision_id, d, served = await policy.evaluate(req.url, req.metadata, clock())
        return {
            "decision_id": decision_id,
            "verdict": served,
            "would_verdict": d.verdict,
            "shadow": config.shadow,
            "source": d.source,
            "reason": d.reason,
            "category": d.category,
            "probs": d.answers,
            "ttl": d.ttl_s,
            "expires_at": d.expires_at,
            "appealable": appealable(parse(req.url)),
        }

    @app.post("/v1/appeal")
    async def appeal(req: AppealRequest):
        try:
            return await policy.appeal(req.decision_id, req.justification, clock())
        except AppealRejected as e:
            body = {"detail": e.detail, "retry_after_s": e.retry_after_s}
            return JSONResponse(body, status_code=e.status)

    @app.post("/v1/heartbeat")
    def heartbeat(req: HeartbeatRequest):
        now = clock()
        db.heartbeat(req.client, req.version, now)
        return {"ok": True, "focus": policy.schedule.is_focus(now)}

    @app.get("/v1/status")
    def status():
        now = clock()
        nxt = policy.schedule.next_change(now)
        return {
            "version": __version__,
            "now": iso(now),
            "focus": policy.schedule.is_focus(now),
            "next_change": iso(nxt) if nxt else None,
            "shadow": config.shadow,
            "schedule": policy.schedule.to_dict(),
            "classifier": {
                "primary": classifier.primary.label,
                "fallback": classifier.fallback.label if classifier.fallback else None,
            },
            "rules": config.rules.model_dump(),
            "appeals": {
                "enabled": policy.judge is not None,
                "min_words": config.appeals.min_words,
                "left_today": policy.appeals_left(now),
            },
            "last_heartbeat": db.last_heartbeat(),
        }

    return app
