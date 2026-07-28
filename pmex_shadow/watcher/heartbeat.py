"""FR-W-8: liveness row written every 5s. Bots halt themselves when it goes stale
(FR-EXE-8) — silence must mean "I am blind," never "nothing is happening."
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg

logger = logging.getLogger("pmex_shadow.watcher.heartbeat")

INTERVAL_S = 5


async def write_heartbeat(conn: asyncpg.Connection, service: str, detail: dict | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO heartbeats (service, at, detail)
        VALUES ($1, now(), $2)
        ON CONFLICT (service) DO UPDATE SET at = EXCLUDED.at, detail = EXCLUDED.detail
        """,
        service,
        json.dumps(detail or {}),
    )


async def run_heartbeat_loop(database_url: str, service: str, detail_fn=lambda: {}) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        while True:
            try:
                await write_heartbeat(conn, service, detail_fn())
            except Exception:
                logger.exception("failed to write heartbeat for %s", service)
            await asyncio.sleep(INTERVAL_S)
    finally:
        await conn.close()
