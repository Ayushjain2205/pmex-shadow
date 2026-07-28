"""Data API poll-per-target sweep (FR-W-6). Backfills what the socket missed during
reconnects and is what makes `sources.chain.enabled: false` viable on its own
(FR-W-7) — never the primary path when chain is enabled, but must independently
discover the same fills the chain path does, with overlap resolved harmlessly by the
`target_fills.dedupe_key` unique constraint.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

import asyncpg
import httpx

from pmex_shadow.watcher.chain import get_watched_targets, insert_fill
from pmex_shadow.watcher.normalize import target_fill_from_dataapi_trade

logger = logging.getLogger("pmex_shadow.watcher.sweep")


async def sweep_once(client: httpx.AsyncClient, conn: asyncpg.Connection, data_api_base_url: str, target: str) -> int:
    url = f"{data_api_base_url.rstrip('/')}/trades"
    resp = await client.get(url, params={"user": target, "limit": 100})
    resp.raise_for_status()
    trades = resp.json()

    inserted = 0
    detected_at = dt.datetime.now(dt.timezone.utc)
    for trade in trades:
        fill = target_fill_from_dataapi_trade(trade, detected_at)
        fill_id = await insert_fill(conn, fill, trade)
        if fill_id is not None:
            inserted += 1
    return inserted


async def run_sweep_loop(database_url: str, data_api_base_url: str, poll_interval_s: int = 30) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                try:
                    targets = await get_watched_targets(conn)
                    total = 0
                    for target in targets:
                        try:
                            total += await sweep_once(client, conn, data_api_base_url, target)
                        except httpx.HTTPError as exc:
                            logger.warning("sweep failed for %s: %s", target, exc)
                    if total:
                        logger.info("sweep inserted %d new fill(s) across %d target(s)", total, len(targets))
                except Exception:
                    logger.exception("sweep loop iteration failed")
                await asyncio.sleep(poll_interval_s)
    finally:
        await conn.close()
