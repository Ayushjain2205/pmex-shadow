"""Shared target-registration logic used by both the CLI (`targets add`) and the
control-plane UI's add-target form, so the two entry points can't drift.
"""

from __future__ import annotations

import json
import re

import asyncpg

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class InvalidAddress(ValueError):
    pass


async def register_target(conn: asyncpg.Connection, address: str, alias: str | None) -> bool:
    """Insert or update a target_stats row. Returns True if this is a newly-seen
    target (a fresh `targets.add` event was logged), False if it already existed.
    New targets start `active` immediately — no shadow/probation period (removed
    2026-07-31 at the repo owner's explicit request; see PRD FR-T-4's amendment
    note for the tradeoff this gives up).
    """
    addr = address.strip().lower()
    if not _ADDRESS_RE.match(addr):
        raise InvalidAddress(f"{address!r} is not a 0x-prefixed 40-hex-char address")

    row = await conn.fetchrow("SELECT status FROM target_stats WHERE target = $1", addr)
    await conn.execute(
        """
        INSERT INTO target_stats (target, alias, status)
        VALUES ($1, $2, 'active')
        ON CONFLICT (target) DO UPDATE SET alias = COALESCE(EXCLUDED.alias, target_stats.alias)
        """,
        addr, alias,
    )
    is_new = row is None
    if is_new:
        await conn.execute(
            "INSERT INTO events (level, component, message, context) VALUES ('INFO', 'targets.add', 'target added, active immediately', $1)",
            json.dumps({"target": addr}),
        )
    return is_new
