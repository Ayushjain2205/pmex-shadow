"""Control plane (FR-C-*). Phase 0: stub app that enforces the binding security
defaults from day one — FR-C-7 (no default credentials, refuse to start without an
auth secret) and FR-C-8 (read-only unless writes are explicitly enabled) — since those
are cheap to get right early and expensive to retrofit onto a running dashboard.

Screens (fleet view, bot detail, targets, params) ship in Phase 6.
"""

from __future__ import annotations

import datetime as dt
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pmex_shadow.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    if not settings.control_auth_secret:
        sys.stderr.write(
            "FATAL: PMEX_CONTROL_AUTH_SECRET is not set. Refusing to start with no "
            "credentials (FR-C-7). Set it in .env and retry.\n"
        )
        raise SystemExit(1)

    app = FastAPI(title="pmex-shadow control")
    app.state.settings = settings
    app.state.started_at = dt.datetime.now(dt.timezone.utc)

    @app.middleware("http")
    async def _block_writes_by_default(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not settings.control_allow_writes:
            return JSONResponse(
                {"error": "read-only: set PMEX_CONTROL_ALLOW_WRITES=1 to enable parameter writes (FR-C-8)"},
                status_code=403,
            )
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "started_at": app.state.started_at.isoformat(),
            "writes_enabled": settings.control_allow_writes,
            "note": "Phase 0 stub — fleet/bot/targets/params screens ship in Phase 6",
        }

    @app.get("/")
    async def root() -> dict:
        return {"service": "pmex-shadow control", "status": "stub — see /healthz"}

    return app
