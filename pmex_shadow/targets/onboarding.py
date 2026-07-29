"""Shadow onboarding (FR-T-4): a new target runs the full pipeline — intents
recorded, orders suppressed — for `shadow_days` before being trusted to trade for
real. §5's `target_stats` schema has no "added_at" column to anchor that clock
against, so the CLI's `targets add` logs an `events` row (component=
'targets.onboarding') at the moment a target is registered, and that row's
timestamp is what this checks elapsed time against — the natural, already-existing
place a "when did we start watching this" fact would live, rather than adding an
unrequested schema field to a table §5 defines exactly.
"""

from __future__ import annotations

import datetime as dt

import asyncpg


async def shadow_started_at(conn: asyncpg.Connection, target: str) -> dt.datetime | None:
    row = await conn.fetchrow(
        "SELECT at FROM events WHERE component = 'targets.onboarding' AND context->>'target' = $1 ORDER BY at ASC LIMIT 1",
        target,
    )
    return row["at"] if row else None


async def check_onboarding(conn: asyncpg.Connection, target: str, shadow_days: int, now: dt.datetime) -> str | None:
    """Returns 'active' if a shadow target has completed its shadow period, else
    None (no change — including if we can't find a shadow-start event at all, since
    guessing when to start trusting a target for real is exactly the wrong place to
    guess).
    """
    row = await conn.fetchrow("SELECT status FROM target_stats WHERE target = $1", target)
    if row is None or row["status"] != "shadow":
        return None

    started_at = await shadow_started_at(conn, target)
    if started_at is None:
        return None

    if (now - started_at).days >= shadow_days:
        return "active"
    return None
