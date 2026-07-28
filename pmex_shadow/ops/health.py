"""Liveness helpers. `heartbeat_age` is Phase 1 scope (FR-EXE-8: bots halt themselves
when the watcher heartbeat is older than `watcher_stale_s` — silence must mean "I am
blind," never "nothing is happening"). Full /metrics exposition ships in Phase 6/7.
"""

from __future__ import annotations

import datetime as dt

import asyncpg


async def heartbeat_age(conn: asyncpg.Connection, service: str) -> dt.timedelta | None:
    """Age of the most recent heartbeat row for `service`, or None if it has never
    reported (distinguish "stale" from "never started" — both are unsafe to trade on,
    but they're different failure modes worth telling apart in logs)."""
    row = await conn.fetchrow("SELECT at FROM heartbeats WHERE service = $1", service)
    if row is None:
        return None
    return dt.datetime.now(dt.timezone.utc) - row["at"]
