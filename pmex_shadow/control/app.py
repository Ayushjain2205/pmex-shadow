"""Control plane (FR-C-*). Binds 127.0.0.1 by default, refuses to start without an
auth secret (FR-C-7), and is read-only unless writes are explicitly enabled
(FR-C-8) — enforced at the middleware layer so no individual route can accidentally
bypass it. Treasury endpoints do not exist here at all, in the web app or otherwise
(design doc §2.2: "never in the web UI") — there is no route for it, not a disabled one.

Screens: fleet view, bot detail, targets, params (FR-C-2). Financial figures are
queried straight from Postgres (control/queries.py, FR-C-1) — this module never
touches Prometheus.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import sys
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pmex_shadow.config import Settings
from pmex_shadow.control import config_write, queries
from pmex_shadow.targets.profile import fetch_profile

TEMPLATES_DIR = Path(__file__).parent / "templates"

_BOT_PATH = re.compile(r"^/bots/[A-Za-z0-9_.-]+$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _return_to(value: str) -> str:
    """Where a stop/resume POST lands. Stopping a bot from its own page and being
    thrown back to the fleet list loses the thing you were looking at, so the form
    says where it came from — but only a bot page is honoured, matched against a
    pattern rather than trusted, so a crafted form can't turn these endpoints into
    an open redirect.
    """
    return value if _BOT_PATH.match(value) else "/"


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
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await app.state.pool.close()

    @app.middleware("http")
    async def _block_writes_by_default(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not settings.control_allow_writes:
            return JSONResponse(
                {"error": "read-only: set PMEX_CONTROL_ALLOW_WRITES=1 to enable parameter writes (FR-C-8)"},
                status_code=403,
            )
        return await call_next(request)

    def _ctx(request: Request, **extra) -> dict:
        return {"request": request, "writes_enabled": settings.control_allow_writes, **extra}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "started_at": app.state.started_at.isoformat(), "writes_enabled": settings.control_allow_writes}

    @app.get("/metrics")
    async def metrics():
        from fastapi import Response

        from pmex_shadow.ops.metrics import render_metrics

        body = await render_metrics(settings.database_url)
        return Response(content=body, media_type="text/plain; version=0.0.4")

    @app.get("/")
    async def fleet(
        request: Request,
        message: str | None = None, error: str | None = None,
        mode: str | None = None, status: str | None = None, archived: int = 0,
    ):
        mode = mode if mode in ("live", "paper", "watch") else None
        status = status if status in queries.FLEET_STATUSES or status == "archived" else None
        # Asking for status=archived without the toggle would return nothing at all,
        # which reads as a bug rather than a filter.
        show_archived = bool(archived) or status == "archived"
        async with app.state.pool.acquire() as conn:
            result = await queries.fleet_view(conn, mode=mode, status=status, show_archived=show_archived)
        return templates.TemplateResponse(
            request, "fleet.html",
            _ctx(request, active_nav="fleet", message=message, error=error,
                 mode=mode, status=status, show_archived=show_archived, **result),
        )

    @app.post("/bots/{bot_id}/archive")
    async def bot_archive_route(request: Request, bot_id: str, force: int = 0):
        from urllib.parse import quote

        from pmex_shadow.ops.registry import archive_blockers, archive_bot

        async with app.state.pool.acquire() as conn:
            blockers = await archive_blockers(conn, bot_id)
            if blockers and not force:
                # Same guardrail as the CLI, same wording — the UI must not be the
                # lenient path to archiving a bot that's still trading.
                return RedirectResponse(
                    f"/?error={quote(f'{bot_id} not archived: ' + '; '.join(blockers))}", status_code=303,
                )
            if not await archive_bot(conn, bot_id, "archived from control UI"):
                return RedirectResponse(f"/?error={quote(f'bot {bot_id} not found')}", status_code=303)
        return RedirectResponse(f"/?message={quote(f'{bot_id} archived')}", status_code=303)

    @app.post("/bots/{bot_id}/unarchive")
    async def bot_unarchive_route(request: Request, bot_id: str):
        from urllib.parse import quote

        from pmex_shadow.ops.registry import unarchive_bot

        async with app.state.pool.acquire() as conn:
            if not await unarchive_bot(conn, bot_id):
                return RedirectResponse(f"/?error={quote(f'bot {bot_id} not found')}", status_code=303)
        return RedirectResponse(f"/?message={quote(f'{bot_id} unarchived — still stopped')}", status_code=303)

    @app.post("/bots/{bot_id}/halt")
    async def bot_halt(request: Request, bot_id: str, return_to: str = Form("")):
        from urllib.parse import quote

        from pmex_shadow.ops.killswitch import halt_bot

        back = _return_to(return_to)
        async with app.state.pool.acquire() as conn:
            current = await config_write.get_active_config(conn, bot_id)
            if current is None:
                return RedirectResponse(f"{back}?error={quote(f'bot {bot_id} not found')}", status_code=303)
            await halt_bot(conn, bot_id, "manual stop (control UI)", flatten=False)
        return RedirectResponse(f"{back}?message={quote(f'{bot_id} halted')}", status_code=303)

    @app.post("/bots/{bot_id}/resume")
    async def bot_resume_route(request: Request, bot_id: str, return_to: str = Form("")):
        from urllib.parse import quote

        from pmex_shadow.ops.killswitch import resume_bot

        back = _return_to(return_to)
        async with app.state.pool.acquire() as conn:
            current = await config_write.get_active_config(conn, bot_id)
            if current is None:
                return RedirectResponse(f"{back}?error={quote(f'bot {bot_id} not found')}", status_code=303)
            await resume_bot(conn, bot_id)
        return RedirectResponse(f"{back}?message={quote(f'{bot_id} resumed')}", status_code=303)

    @app.get("/bots/{bot_id}")
    async def bot_detail(
        request: Request, bot_id: str, pos_page: int = 1, dec_page: int = 1, log_page: int = 1,
        message: str | None = None, error: str | None = None,
    ):
        import json as _json

        async with app.state.pool.acquire() as conn:
            detail = await queries.bot_detail(conn, bot_id, settings.gamma_api_base_url, pos_page, dec_page, log_page)
        equity_curve_json = _json.dumps([
            {"t": row["bucket"].isoformat(), "v": float(row["cumulative_pnl"])} for row in detail["equity_curve"]
        ])
        return templates.TemplateResponse(
            request, "bot_detail.html",
            _ctx(request, bot_id=bot_id, active_nav="fleet", **detail,
                 message=message, error=error, equity_curve_json=equity_curve_json),
        )

    @app.get("/targets")
    async def targets(request: Request, message: str | None = None, error: str | None = None):
        async with app.state.pool.acquire() as conn:
            result = await queries.targets_view(conn)
            bot_ids = await queries.list_bot_ids(conn)
        return templates.TemplateResponse(
            request, "targets.html",
            _ctx(request, bot_ids=bot_ids, active_nav="targets", message=message, error=error, **result),
        )

    @app.post("/targets")
    async def targets_add(
        request: Request,
        address: str = Form(...), alias: str = Form(""),
        bot_id: str = Form(""),
    ):
        from urllib.parse import quote

        from pmex_shadow.targets.registry import InvalidAddress, register_target

        alias = alias.strip() or None
        bot_id = bot_id.strip() or None

        async with app.state.pool.acquire() as conn:
            try:
                is_new = await register_target(conn, address, alias)
            except InvalidAddress as exc:
                return RedirectResponse(f"/targets?error={quote(str(exc))}", status_code=303)

            verb = "now tracking" if is_new else "alias updated for"
            message = f"{verb} {address.strip().lower()}"

            if bot_id:
                current = await config_write.get_active_config(conn, bot_id)
                if current is None:
                    return RedirectResponse(f"/targets?message={quote(message)}&error={quote(f'bot {bot_id} not found — target was still registered')}", status_code=303)
                ref = alias or address.strip().lower()
                new_config = dict(current["config"])
                existing_targets = list(new_config.get("targets", []))
                if ref not in existing_targets:
                    new_config["targets"] = existing_targets + [ref]
                    applied, reason = await config_write.propose_config_update(conn, bot_id, actor="control-ui", new_config_dict=new_config)
                    if applied:
                        message += f", attached to {bot_id} (v{current['version'] + 1})"
                    else:
                        return RedirectResponse(f"/targets?message={quote(message)}&error={quote(f'could not attach to {bot_id}: {reason}')}", status_code=303)
                else:
                    message += f", already in {bot_id}'s target list"

        return RedirectResponse(f"/targets?message={quote(message)}", status_code=303)

    @app.get("/targets/{address}")
    async def target_detail(request: Request, address: str, activity_page: int = 1):
        """A target wallet's profile: their real trading history from Polymarket's
        Data API, alongside what our bots decided about the slice of it we saw.

        The two halves fail independently on purpose — the Data API is a live
        third-party call and the Postgres half isn't, so a slow or down Data API
        costs you the profile panels and nothing else.
        """
        if not _ADDRESS.match(address):
            raise HTTPException(status_code=404, detail="not a wallet address")
        addr = address.lower()

        async with app.state.pool.acquire() as conn:
            detail, profile = await asyncio.gather(
                queries.target_detail(conn, addr),
                fetch_profile(settings.data_api_base_url, addr, activity_page),
            )
            if detail is None:
                raise HTTPException(status_code=404, detail=f"{addr} is not a registered target")
            # Only the markets on the visible activity page need a decision lookup —
            # this is why the feed is paginated rather than rendered whole.
            our_calls = await queries.decisions_for_tokens(
                conn, addr, [a["asset"] for a in profile["activity"] if a.get("asset")],
            )

        return templates.TemplateResponse(
            request, "target_detail.html",
            _ctx(request, active_nav="targets", profile=profile, our_calls=our_calls, **detail),
        )

    @app.get("/analysis")
    async def analysis(request: Request):
        async with app.state.pool.acquire() as conn:
            result = await queries.latency_view(conn)
        return templates.TemplateResponse(request, "analysis.html", _ctx(request, active_nav="analysis", **result))

    @app.get("/logs")
    async def logs(request: Request, bot_id: str | None = None, component: str | None = None, level: str | None = None, page: int = 1):
        async with app.state.pool.acquire() as conn:
            result = await queries.logs_view(conn, bot_id, component, level, page)
        return templates.TemplateResponse(request, "logs.html", _ctx(request, active_nav="logs", **result))

    @app.get("/bots/{bot_id}/params")
    async def params_form(request: Request, bot_id: str, message: str | None = None, applied: bool | None = None):
        async with app.state.pool.acquire() as conn:
            current = await config_write.get_active_config(conn, bot_id)
            audit = await conn.fetch(
                "SELECT actor, from_version, to_version, outcome, reason, at FROM config_audit WHERE bot_id = $1 ORDER BY at DESC LIMIT 20",
                bot_id,
            )
        if current is None:
            return templates.TemplateResponse(request, "logs.html", _ctx(request, logs=[], active_nav="fleet"), status_code=404)
        return templates.TemplateResponse(
            request, "params.html",
            _ctx(request, bot_id=bot_id, config=current["config"], version=current["version"], audit=audit, message=message, applied=applied),
        )

    @app.post("/bots/{bot_id}/params")
    async def params_submit(
        request: Request, bot_id: str,
        mode: str = Form(...), policy_profile: str = Form(...), envelope_usd: str = Form(...), categories: str = Form(""),
        targets: str = Form(""),
    ):
        async with app.state.pool.acquire() as conn:
            current = await config_write.get_active_config(conn, bot_id)
            if current is None:
                return RedirectResponse(f"/bots/{bot_id}/params?applied=false&message=bot+not+found", status_code=303)
            new_config = dict(current["config"])
            new_config["mode"] = mode
            new_config["policy"] = {"profile": policy_profile}
            new_config["risk"] = {"envelope_usd": envelope_usd}
            cats = [c.strip() for c in categories.split(",") if c.strip()]
            new_config["selectors"] = {**current["config"].get("selectors", {}), "categories": cats or None}
            new_config["targets"] = [t.strip() for t in targets.split(",") if t.strip()]

            applied, message = await config_write.propose_config_update(conn, bot_id, actor="control-ui", new_config_dict=new_config)
        return RedirectResponse(f"/bots/{bot_id}/params?applied={str(applied).lower()}&message={message}", status_code=303)

    return app
