"""Financial and operational reads for the control plane (FR-C-1): PnL, positions,
fills, and equity data come straight from Postgres — never sampled, never through
Prometheus. This module is the only place the control app touches the database.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal

import asyncpg
import httpx


def _jsonb(value):
    """asyncpg returns JSONB columns as raw text, not a decoded object, without an
    explicit type codec registered on the connection — found the hard way (a live
    AttributeError in the fleet view) rather than assumed. Every JSONB read in this
    module goes through this rather than trusting the driver to have decoded it.
    """
    return value if isinstance(value, dict) else json.loads(value)


# (human label, category) per PRD §6 skip reason. Category drives color in the UI —
# Seven categories, not three — three collapsed slippage_guard/stale_fill/
# volatility_guard (the reasons that actually dominate real skip volume) into one
# identical color, which looked exactly as monochrome as no categorization at all.
# Each category below maps to its own hue in base.html's .pill-cat-* — capital
# (red), position slots (teal), operational halt (slate), timing (yellow),
# price slipped (orange), market chaos (rose), everything filtered by a
# selector/sizing rule by design (violet), and data_gap (sky) — added alongside
# no_orderbook/no_market_meta/target_not_registered: these aren't a decide() verdict
# at all, they're consumer.py giving up *before* decide() runs because a lookup came
# back empty. Previously that was a bare `return` with nothing recorded anywhere;
# now it's a real row like everything else, so it needed its own category rather
# than being misfiled under "filtered" (which is specifically selector/sizing rules).
_SKIP_REASON_META: dict[str, tuple[str, str]] = {
    "envelope_exhausted": ("Capital exhausted", "capital"),
    "global_exposure_cap": ("Fleet exposure cap hit", "capital"),
    "max_concurrent_positions": ("Position limit reached", "position_limit"),
    "bot_halted": ("Bot halted", "halted"),
    "target_paused": ("Target paused", "halted"),
    "stale_fill": ("Fill too old", "timing"),
    "slippage_guard": ("Price moved too far", "slippage"),
    "volatility_guard": ("Market too volatile", "chaos"),
    "selector_category": ("Category filtered", "filtered"),
    "selector_liquidity": ("Book too thin", "filtered"),
    "selector_notional": ("Target trade too small", "filtered"),
    "selector_resolution_window": ("Resolves too far out", "filtered"),
    "unknown_category": ("Category unknown", "filtered"),
    "market_not_tradeable": ("Market not tradeable", "filtered"),
    "below_target_percentile": ("Below sizing threshold", "filtered"),
    "below_min_order": ("Order too small", "filtered"),
    "no_position_to_exit": ("Nothing to sell", "filtered"),
    "netted_out": ("Netted against another intent", "filtered"),
    "no_orderbook": ("No orderbook available", "data_gap"),
    "no_market_meta": ("Market metadata unavailable", "data_gap"),
    "target_not_registered": ("Target not registered yet", "data_gap"),
}


def _skip_label(reason: str) -> str:
    return _SKIP_REASON_META.get(reason, (reason, "filtered"))[0]


def _skip_category(reason: str) -> str:
    return _SKIP_REASON_META.get(reason, (reason, "filtered"))[1]


def _skip_summary(reason: str, detail: dict | None) -> str | None:
    """One human-readable line of the actual numbers behind a skip — the whole
    point of Skip.detail existing. Returns None where the label alone already says
    everything (bot_halted, no_position_to_exit, etc.) rather than padding with
    nothing.
    """
    if not detail:
        return None
    if "reason" in detail and len(detail) == 1:
        return {"no_liquidity_on_book_side": "No liquidity on that side of the book",
                "no_current_book_to_compare": "No current book to compare against",
                "no_history_in_window": "No book history in the window yet"}.get(detail["reason"], detail["reason"])
    if reason == "stale_fill":
        return f"{detail['age_s']:.0f}s old (limit {detail['max_fill_age_s']}s)"
    if reason == "slippage_guard":
        return f"{detail['adverse_ticks']:.1f} ticks adverse — target ${detail['target_price']}, best ${detail['best_price']} (limit {detail['max_slippage_ticks']} ticks)"
    if reason == "volatility_guard":
        return f"{detail['move_ticks']:.1f} ticks moved in {detail['window_s']}s (limit {detail['max_ticks']})"
    if reason == "envelope_exhausted":
        return f"${detail['available_usd']:.2f} available (envelope ${detail['envelope_usd']:.2f}, realized ${detail['realized_pnl_usd']:.2f})"
    if reason == "global_exposure_cap":
        return f"${detail['global_exposure_usd']:.2f} / ${detail['global_max_exposure_usd']:.2f} fleet-wide"
    if reason == "max_concurrent_positions":
        return f"{detail['current_open_positions']} / {detail['max_concurrent_positions']} positions open"
    if reason == "below_target_percentile":
        return f"{detail['target_percentile']:.1f}th percentile (need {detail['min_required_percentile']:.1f}th+)"
    if reason == "below_min_order":
        return f"${detail['sized_usd']:.2f} sized (min ${detail['min_order_usd']:.2f})"
    if reason == "selector_category":
        return f"{detail['market_category']!r} not in {detail['allowed_categories']}"
    if reason == "selector_liquidity":
        return f"${detail['book_liquidity_usd']:.2f} liquidity (need ${detail['min_required_usd']:.2f}+)"
    if reason == "selector_notional":
        return f"${detail['fill_notional_usd']:.2f} fill (need ${detail['min_required_usd']:.2f}+)"
    if reason == "selector_resolution_window":
        return f"{detail['resolution_days_out']}d out (limit {detail['max_allowed_days']}d)"
    if reason == "target_paused":
        return f"status: {detail['target_status']}"
    if reason in ("no_orderbook", "no_market_meta"):
        return f"token {detail['token_id'][:12]}…"
    if reason == "target_not_registered":
        return f"{detail['target']} not in target_stats"
    return " · ".join(f"{k}: {v}" for k, v in detail.items())


def _context_summary(context: dict | None) -> str | None:
    """`events.context` has no per-message format the way skip_detail has per-reason
    (events come from a handful of unrelated components, not one policy engine) — a
    flat `key: value` join is honest rather than guessing a nicer format per message."""
    if not context:
        return None
    return " · ".join(f"{k}: {v}" for k, v in context.items())


BOT_HEARTBEAT_STALE_AFTER = dt.timedelta(seconds=60)  # bots write one every 5s (watcher/heartbeat.py); 60s is generous


async def fleet_view(conn: asyncpg.Connection) -> dict:
    """Per bot: mode, watcher-relative lag, exposure vs envelope, today's PnL, skip
    rate, last fill — the fleet-view screen (design doc §3.8)."""
    bots = await conn.fetch(
        """
        SELECT bc.bot_id, bc.version, bc.config, bc.active
        FROM bot_config bc
        WHERE bc.active
        ORDER BY bc.bot_id
        """
    )
    rows = []
    for b in bots:
        bot_id = b["bot_id"]
        config = _jsonb(b["config"])

        heartbeat = await conn.fetchrow("SELECT at FROM heartbeats WHERE service = $1", f"bot:{bot_id}")
        deployed = await conn.fetchrow(
            "SELECT COALESCE(sum(cost_basis_usd), 0) AS deployed FROM positions WHERE bot_id = $1 AND lifecycle NOT IN ('redeemed', 'refunded', 'written_off')",
            bot_id,
        )
        today_pnl = await conn.fetchrow(
            "SELECT COALESCE(sum(realized_pnl_usd), 0) AS pnl FROM positions WHERE bot_id = $1 AND last_event_at >= date_trunc('day', now())",
            bot_id,
        )
        counts = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE decision = 'COPY') AS copies, count(*) FILTER (WHERE decision = 'SKIP') AS skips "
            "FROM intents WHERE bot_id = $1 AND created_at >= now() - interval '24 hours'",
            bot_id,
        )
        last_fill = await conn.fetchrow(
            "SELECT max(created_at) AS at FROM intents WHERE bot_id = $1", bot_id,
        )
        total = (counts["copies"] or 0) + (counts["skips"] or 0)
        skip_rate = (counts["skips"] / total) if total > 0 else None

        heartbeat_at = heartbeat["at"] if heartbeat else None
        is_stale = heartbeat_at is None or dt.datetime.now(dt.timezone.utc) - heartbeat_at > BOT_HEARTBEAT_STALE_AFTER

        # Mirrors ledger/subaccount.py's get_ledger_state() halted check exactly
        # (same events, same "most recent halt vs most recent resume" rule) — not
        # imported from there since that function also pulls positions/exposure
        # this view doesn't need; duplicated as one query pair, not re-derived
        # differently.
        last_halt = await conn.fetchrow(
            "SELECT component, message, context, at FROM events WHERE bot_id = $1 AND level = 'CRITICAL' "
            "AND component IN ('ledger.reconcile', 'killswitch') ORDER BY at DESC LIMIT 1",
            bot_id,
        )
        last_resume = await conn.fetchrow(
            "SELECT at FROM events WHERE bot_id = $1 AND component = 'killswitch' AND message = 'resumed' ORDER BY at DESC LIMIT 1",
            bot_id,
        )
        halted = last_halt is not None and (last_resume is None or last_halt["at"] > last_resume["at"])
        halt_reason = None
        if halted:
            ctx = _jsonb(last_halt["context"]) if last_halt["context"] is not None else {}
            halt_reason = ctx.get("reason") or last_halt["message"]

        rows.append({
            "bot_id": bot_id,
            "mode": config.get("mode"),
            "version": b["version"],
            "envelope_usd": config.get("risk", {}).get("envelope_usd"),
            "deployed_usd": deployed["deployed"],
            "today_pnl_usd": today_pnl["pnl"],
            "skip_rate_24h": skip_rate,
            "halted": halted,
            "halt_reason": halt_reason,
            "last_fill_at": last_fill["at"],
            "heartbeat_at": heartbeat_at,
            "is_stale": is_stale,
        })

    summary = {
        "count": len(rows),
        "active_count": sum(1 for r in rows if not r["halted"]),
        "halted_count": sum(1 for r in rows if r["halted"]),
        "stale_count": sum(1 for r in rows if r["is_stale"]),
        "live_count": sum(1 for r in rows if r["mode"] == "live"),
        "paper_count": sum(1 for r in rows if r["mode"] == "paper"),
        "watch_count": sum(1 for r in rows if r["mode"] == "watch"),
        "total_today_pnl_usd": sum((r["today_pnl_usd"] for r in rows), Decimal(0)),
    }
    return {"bots": rows, "summary": summary}


async def _fetch_market_question(client: httpx.AsyncClient, gamma_api_base_url: str, token_id: str) -> str | None:
    """Two real, mutually exclusive states to check, not one: querying
    `clob_token_ids=<id>` with no `closed` param returns only *open* markets, and
    `closed=true` returns *only* closed ones (confirmed live: an open market's token
    queried with `closed=true` comes back empty, not "includes it anyway"). A
    resolved-2-minutes-ago 5-minute crypto market genuinely needs the second call —
    found by testing against a real position the first version of this function
    still got wrong, not by reasoning about it in the abstract.
    """
    base = f"{gamma_api_base_url.rstrip('/')}/markets"
    resp = await client.get(base, params={"clob_token_ids": token_id})
    if resp.status_code == 200 and resp.json():
        return resp.json()[0].get("question")
    resp = await client.get(base, params={"clob_token_ids": token_id, "closed": "true"})
    if resp.status_code == 200 and resp.json():
        return resp.json()[0].get("question")
    return None


async def get_market_titles(gamma_api_base_url: str, token_ids: set[str]) -> dict[str, str]:
    """token_id alone (a long numeric CLOB id) means nothing to a human looking at
    the dashboard — this resolves it to the actual market question via Gamma.

    One request per token per state, not a comma-separated batch: a batch looked
    like it worked in an initial check (two of the *same* token, comma-joined,
    returned two rows) but two genuinely *different* token ids in one call returns
    `{"type": "validation error", "error": "invalid clob token ids"}` — confirmed
    live, and only caught because the dashboard was actually showing the wrong thing
    for a real position, not because the first test was thorough enough.
    """
    if not token_ids:
        return {}
    titles: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=10) as client:
        results = await asyncio.gather(*(
            _fetch_market_question(client, gamma_api_base_url, tid) for tid in token_ids
        ), return_exceptions=True)
    for tid, question in zip(token_ids, results):
        if isinstance(question, Exception) or question is None:
            continue
        titles[tid] = question
    return titles


POSITIONS_PAGE_SIZE = 20
DECISIONS_PAGE_SIZE = 25
LOGS_PAGE_SIZE = 30


def _page_info(page: int, page_size: int, total: int) -> dict:
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(max(page, 1), total_pages)
    return {"page": page, "page_size": page_size, "total": total, "total_pages": total_pages, "offset": (page - 1) * page_size}


async def bot_detail(
    conn: asyncpg.Connection, bot_id: str, gamma_api_base_url: str,
    pos_page: int = 1, dec_page: int = 1, log_page: int = 1,
) -> dict:
    pos_total = await conn.fetchval("SELECT count(*) FROM positions WHERE bot_id = $1", bot_id)
    pos_pg = _page_info(pos_page, POSITIONS_PAGE_SIZE, pos_total)
    positions = [dict(r) for r in await conn.fetch(
        "SELECT token_id, shares, cost_basis_usd, realized_pnl_usd, lifecycle FROM positions "
        "WHERE bot_id = $1 ORDER BY last_event_at DESC LIMIT $2 OFFSET $3",
        bot_id, pos_pg["page_size"], pos_pg["offset"],
    )]

    dec_total = await conn.fetchval("SELECT count(*) FROM intents WHERE bot_id = $1", bot_id)
    dec_pg = _page_info(dec_page, DECISIONS_PAGE_SIZE, dec_total)
    recent_intents = [dict(r) for r in await conn.fetch(
        """
        SELECT i.decision, i.skip_reason, i.skip_detail, i.token_id, i.side, i.target_price, i.intended_price,
               i.intended_shares, i.target_percentile, i.created_at
        FROM intents i WHERE i.bot_id = $1 ORDER BY i.created_at DESC LIMIT $2 OFFSET $3
        """,
        bot_id, dec_pg["page_size"], dec_pg["offset"],
    )]
    for i in recent_intents:
        if i["skip_detail"] is not None:
            i["skip_detail"] = _jsonb(i["skip_detail"])
        if i["skip_reason"]:
            i["skip_label"] = _skip_label(i["skip_reason"])
            i["skip_category"] = _skip_category(i["skip_reason"])
            i["skip_summary"] = _skip_summary(i["skip_reason"], i["skip_detail"])
    skips_by_reason = [dict(r) for r in await conn.fetch(
        "SELECT skip_reason, count(*) AS n FROM intents WHERE bot_id = $1 AND decision = 'SKIP' "
        "AND created_at >= now() - interval '7 days' GROUP BY skip_reason ORDER BY n DESC",
        bot_id,
    )]
    for s in skips_by_reason:
        s["skip_label"] = _skip_label(s["skip_reason"])
        s["skip_category"] = _skip_category(s["skip_reason"])
    equity_curve = await conn.fetch(
        """
        SELECT bucket, sum(pnl) OVER (ORDER BY bucket) AS cumulative_pnl
        FROM (
            SELECT date_trunc('hour', last_event_at) AS bucket, sum(realized_pnl_usd) AS pnl
            FROM positions WHERE bot_id = $1 GROUP BY bucket
        ) hourly
        ORDER BY bucket
        """,
        bot_id,
    )
    log_total = await conn.fetchval("SELECT count(*) FROM events WHERE bot_id = $1", bot_id)
    log_pg = _page_info(log_page, LOGS_PAGE_SIZE, log_total)
    logs = [dict(r) for r in await conn.fetch(
        "SELECT level, component, message, context, at FROM events WHERE bot_id = $1 ORDER BY at DESC LIMIT $2 OFFSET $3",
        bot_id, log_pg["page_size"], log_pg["offset"],
    )]
    for l in logs:
        l["context"] = _jsonb(l["context"]) if l["context"] is not None else None
        l["context_summary"] = _context_summary(l["context"])

    config_row = await conn.fetchrow("SELECT config FROM bot_config WHERE bot_id = $1 AND active", bot_id)
    envelope_usd = _jsonb(config_row["config"]).get("risk", {}).get("envelope_usd") if config_row else None

    summary = await conn.fetchrow(
        """
        SELECT
            COALESCE(sum(realized_pnl_usd), 0) AS total_realized_pnl,
            COALESCE(sum(cost_basis_usd) FILTER (WHERE lifecycle = 'open'), 0) AS deployed_usd,
            count(*) FILTER (WHERE lifecycle IN ('redeemed', 'written_off', 'refunded') AND realized_pnl_usd > 0) AS wins,
            count(*) FILTER (WHERE lifecycle IN ('redeemed', 'written_off', 'refunded') AND realized_pnl_usd <= 0) AS losses
        FROM positions WHERE bot_id = $1
        """,
        bot_id,
    )
    closed_trades = summary["wins"] + summary["losses"]

    token_ids = {p["token_id"] for p in positions} | {i["token_id"] for i in recent_intents}
    titles = await get_market_titles(gamma_api_base_url, token_ids)
    for p in positions:
        p["market_title"] = titles.get(p["token_id"])
    for i in recent_intents:
        i["market_title"] = titles.get(i["token_id"])

    return {
        "positions": positions,
        "recent_intents": recent_intents,
        "skips_by_reason": skips_by_reason,
        "equity_curve": equity_curve,
        "logs": logs,
        "envelope_usd": envelope_usd,
        "total_realized_pnl": summary["total_realized_pnl"],
        "deployed_usd": summary["deployed_usd"],
        "wins": summary["wins"],
        "losses": summary["losses"],
        "closed_trades": closed_trades,
        "win_rate": (summary["wins"] / closed_trades) if closed_trades > 0 else None,
        "pos_pg": pos_pg, "dec_pg": dec_pg, "log_pg": log_pg,
    }


async def list_bot_ids(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch("SELECT bot_id FROM bot_config WHERE active ORDER BY bot_id")
    return [r["bot_id"] for r in rows]


STATS_STALE_AFTER = dt.timedelta(minutes=30)  # targets-recompute runs every 15 minutes; 2x that before flagging


async def targets_view(conn: asyncpg.Connection) -> dict:
    rows = await conn.fetch(
        "SELECT target, alias, status, size_p50, size_p80, size_p95, fills_30d, "
        "hit_rate_30d, pnl_30d_usd, reversal_rate, last_fill_at, computed_at FROM target_stats ORDER BY target"
    )
    stalest_computed_at = min((r["computed_at"] for r in rows), default=None)
    stats_stale = stalest_computed_at is not None and dt.datetime.now(dt.timezone.utc) - stalest_computed_at > STATS_STALE_AFTER
    return {"targets": rows, "stalest_computed_at": stalest_computed_at, "stats_stale": stats_stale}


async def logs_view(
    conn: asyncpg.Connection, bot_id: str | None, component: str | None, level: str | None, page: int = 1,
) -> dict:
    """The global /logs page (FR-C-2). `component` defaults to excluding the `paper`
    research logger — it's ~98% of raw event volume in practice (one row per
    simulated fill, across every fill of every watched target, not just this bot's
    own decisions) and drowns out the handful of actually operational rows
    (watcher.chain reconnects, bot.health halts) that make this page worth looking
    at. `component="all"` opts back in. Found investigating a report that this page
    was unreadable — it was, but the underlying rows (context especially) were
    already fine; nothing here was previously rendered or filterable at all.
    """
    all_components = [r["component"] for r in await conn.fetch("SELECT DISTINCT component FROM events ORDER BY component")]
    all_levels = [r["level"] for r in await conn.fetch("SELECT DISTINCT level FROM events ORDER BY level")]

    where = []
    params: list = []
    if bot_id:
        params.append(bot_id)
        where.append(f"bot_id = ${len(params)}")
    if component == "all":
        pass
    elif component:
        params.append(component)
        where.append(f"component = ${len(params)}")
    else:
        params.append("paper")
        where.append(f"component != ${len(params)}")
    if level:
        params.append(level)
        where.append(f"level = ${len(params)}")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total = await conn.fetchval(f"SELECT count(*) FROM events {where_sql}", *params)
    pg = _page_info(page, LOGS_PAGE_SIZE, total)
    rows = await conn.fetch(
        f"SELECT bot_id, level, component, message, context, at FROM events {where_sql} "
        f"ORDER BY at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}",
        *params, pg["page_size"], pg["offset"],
    )
    logs = [dict(r) for r in rows]
    for l in logs:
        l["context"] = _jsonb(l["context"]) if l["context"] is not None else None
        l["context_summary"] = _context_summary(l["context"])

    return {
        "logs": logs, "pg": pg,
        "all_components": all_components, "all_levels": all_levels,
        "component": component, "level": level, "bot_id": bot_id,
    }
