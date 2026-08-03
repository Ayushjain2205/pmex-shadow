"""Bot lifecycle: which bots are part of the fleet, independent of config versioning.

Three orthogonal states, easily confused:
  * `bot_config.active` — which config *version* is current. A version pointer,
    enforced by the partial unique index on (bot_id) WHERE active. Never means
    "running", never means "in the fleet".
  * halted — operationally paused, reversible via `bot resume`. Derived from
    events, not stored (see ops/killswitch.py).
  * archived (here) — retired from the fleet. History stays queryable and the bot
    stays addressable by bot_id everywhere; it just stops being presented as
    something you'd operate.

Archiving deliberately does NOT clear `bot_config.active`. Doing so would unset
the bot's current config and make it disappear from list_bot_ids() — the picker
that its retained intents/positions are reachable through.
"""

from __future__ import annotations

import datetime as dt

import asyncpg

# A bot writes a heartbeat every 5s (watcher/heartbeat.py). Same 60s grace the
# fleet view uses to call a heartbeat stale, so "archive says it's running" and
# "the fleet view says it's running" can never disagree.
RUNNING_HEARTBEAT_WITHIN = dt.timedelta(seconds=60)


async def register_bot(conn: asyncpg.Connection, bot_id: str) -> None:
    """Idempotent. Re-registering an archived bot does not un-archive it — that is
    an explicit operator action, so a stray restart can't quietly resurrect a
    retired bot into the fleet view."""
    await conn.execute(
        "INSERT INTO bots (bot_id) VALUES ($1) ON CONFLICT (bot_id) DO NOTHING", bot_id
    )


async def archive_blockers(conn: asyncpg.Connection, bot_id: str) -> list[str]:
    """Reasons this bot shouldn't be archived right now. Empty list = safe.

    Advisory, not enforcement: archiving is a presentation change, so --force is a
    legitimate answer to any of these. It exists so you can't archive a bot that's
    still trading without being told.
    """
    blockers: list[str] = []

    heartbeat = await conn.fetchrow("SELECT at FROM heartbeats WHERE service = $1", f"bot:{bot_id}")
    if heartbeat is not None:
        age = dt.datetime.now(dt.timezone.utc) - heartbeat["at"]
        if age < RUNNING_HEARTBEAT_WITHIN:
            blockers.append(f"still running — heartbeat {age.total_seconds():.0f}s ago")

    open_pos = await conn.fetchrow(
        "SELECT count(*) AS n, COALESCE(sum(cost_basis_usd), 0) AS usd FROM positions "
        "WHERE bot_id = $1 AND lifecycle NOT IN ('redeemed', 'refunded', 'written_off', 'voided')",
        bot_id,
    )
    if open_pos["n"]:
        blockers.append(f"{open_pos['n']} position(s) not yet terminal (${open_pos['usd']:.2f} deployed)")

    return blockers


async def archive_bot(conn: asyncpg.Connection, bot_id: str, reason: str | None = None) -> bool:
    """Returns False if the bot_id is unknown. Archiving an already-archived bot
    keeps the original archived_at — it is not a re-archive."""
    row = await conn.fetchrow(
        "UPDATE bots SET archived_at = COALESCE(archived_at, now()), "
        "archived_reason = COALESCE(archived_reason, $2) WHERE bot_id = $1 RETURNING bot_id",
        bot_id, reason,
    )
    return row is not None


async def unarchive_bot(conn: asyncpg.Connection, bot_id: str) -> bool:
    row = await conn.fetchrow(
        "UPDATE bots SET archived_at = NULL, archived_reason = NULL WHERE bot_id = $1 RETURNING bot_id",
        bot_id,
    )
    return row is not None


async def list_bots(conn: asyncpg.Connection) -> list[dict]:
    """Every registered bot, archived or not, newest-registered last."""
    rows = await conn.fetch(
        "SELECT bot_id, created_at, archived_at, archived_reason FROM bots ORDER BY bot_id"
    )
    return [dict(r) for r in rows]
