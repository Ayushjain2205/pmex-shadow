"""Financial and operational reads for the control plane (FR-C-1): PnL, positions,
fills, and equity data come straight from Postgres — never sampled, never through
Prometheus. This module is the only place the control app touches the database.
"""

from __future__ import annotations

import datetime as dt
import json

import asyncpg


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


async def bot_detail(conn: asyncpg.Connection, bot_id: str) -> dict:
    positions = await conn.fetch(
        "SELECT token_id, shares, cost_basis_usd, realized_pnl_usd, lifecycle FROM positions WHERE bot_id = $1 ORDER BY last_event_at DESC LIMIT 50",
        bot_id,
    )
    recent_intents = await conn.fetch(
        """
        SELECT i.decision, i.skip_reason, i.token_id, i.side, i.target_price, i.intended_price,
               i.intended_shares, i.target_percentile, i.created_at
        FROM intents i WHERE i.bot_id = $1 ORDER BY i.created_at DESC LIMIT 50
        """,
        bot_id,
    )
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
    return {
        "positions": positions,
        "recent_intents": recent_intents,
        "skips_by_reason": skips_by_reason,
        "equity_curve": equity_curve,
        "logs": logs,
    }


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
