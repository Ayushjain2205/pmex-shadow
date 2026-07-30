"""Financial and operational reads for the control plane (FR-C-1): PnL, positions,
fills, and equity data come straight from Postgres — never sampled, never through
Prometheus. This module is the only place the control app touches the database.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import asyncpg
import httpx


def _jsonb(value):
    """asyncpg returns JSONB columns as raw text, not a decoded object, without an
    explicit type codec registered on the connection — found the hard way (a live
    AttributeError in the fleet view) rather than assumed. Every JSONB read in this
    module goes through this rather than trusting the driver to have decoded it.
    """
    return value if isinstance(value, dict) else json.loads(value)


async def fleet_view(conn: asyncpg.Connection) -> list[dict]:
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

        rows.append({
            "bot_id": bot_id,
            "mode": config.get("mode"),
            "version": b["version"],
            "envelope_usd": config.get("risk", {}).get("envelope_usd"),
            "deployed_usd": deployed["deployed"],
            "today_pnl_usd": today_pnl["pnl"],
            "skip_rate_24h": skip_rate,
            "last_fill_at": last_fill["at"],
            "heartbeat_at": heartbeat["at"] if heartbeat else None,
        })
    return rows


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


async def bot_detail(conn: asyncpg.Connection, bot_id: str, gamma_api_base_url: str) -> dict:
    positions = [dict(r) for r in await conn.fetch(
        "SELECT token_id, shares, cost_basis_usd, realized_pnl_usd, lifecycle FROM positions WHERE bot_id = $1 ORDER BY last_event_at DESC LIMIT 50",
        bot_id,
    )]
    recent_intents = [dict(r) for r in await conn.fetch(
        """
        SELECT i.decision, i.skip_reason, i.token_id, i.side, i.target_price, i.intended_price,
               i.intended_shares, i.target_percentile, i.created_at
        FROM intents i WHERE i.bot_id = $1 ORDER BY i.created_at DESC LIMIT 50
        """,
        bot_id,
    )]
    skips_by_reason = await conn.fetch(
        "SELECT skip_reason, count(*) AS n FROM intents WHERE bot_id = $1 AND decision = 'SKIP' "
        "AND created_at >= now() - interval '7 days' GROUP BY skip_reason ORDER BY n DESC",
        bot_id,
    )
    equity_curve = await conn.fetch(
        """
        SELECT date_trunc('hour', last_event_at) AS bucket, sum(realized_pnl_usd) AS pnl
        FROM positions WHERE bot_id = $1 GROUP BY bucket ORDER BY bucket
        """,
        bot_id,
    )
    logs = await conn.fetch(
        "SELECT level, component, message, context, at FROM events WHERE bot_id = $1 ORDER BY at DESC LIMIT 100",
        bot_id,
    )

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
    }


async def list_bot_ids(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch("SELECT bot_id FROM bot_config WHERE active ORDER BY bot_id")
    return [r["bot_id"] for r in rows]


async def targets_view(conn: asyncpg.Connection) -> list[dict]:
    return await conn.fetch(
        "SELECT target, alias, status, size_p50, size_p80, size_p95, fills_30d, "
        "hit_rate_30d, pnl_30d_usd, reversal_rate, last_fill_at FROM target_stats ORDER BY target"
    )


async def logs_view(conn: asyncpg.Connection, bot_id: str | None, limit: int = 200) -> list[dict]:
    if bot_id:
        return await conn.fetch(
            "SELECT bot_id, level, component, message, context, at FROM events WHERE bot_id = $1 ORDER BY at DESC LIMIT $2",
            bot_id, limit,
        )
    return await conn.fetch(
        "SELECT bot_id, level, component, message, context, at FROM events ORDER BY at DESC LIMIT $1", limit,
    )
