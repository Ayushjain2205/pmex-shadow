"""Paper logger (Phase 1 slice of FR-EXE-9): for every captured fill, snapshot the
book and record the VWAP a copier would have gotten filling the target's own notional
right now. Not the full paper-mode pipeline from the design doc §3.4a (that needs
Phase 2's sizing/guards and Phase 3's order state machine) — this is the narrower Phase
1 deliverable: "would copying this fill, at this size, have been executable, and at
what slippage." That's exactly the data `analyze` (Phase 2) needs to tell whether a
target's edge survives a ~2s copy delay.

Logged to `events` (component="paper") rather than a new table — §5 doesn't define a
dedicated table for this, and `events` already exists precisely for structured,
JSONB-context log records the UI can query.
"""

from __future__ import annotations

import json
import logging

import asyncpg

from pmex_shadow.market.cache import get_orderbook
from pmex_shadow.models import Side

logger = logging.getLogger("pmex_shadow.research.paper")


async def simulate_and_log_fill(conn: asyncpg.Connection, clob_base_url: str, fill_id: int) -> None:
    row = await conn.fetchrow(
        "SELECT id, target, token_id, side, price, size, notional_usd, block_ts FROM target_fills WHERE id = $1",
        fill_id,
    )
    if row is None:
        logger.warning("paper logger: fill id %d not found", fill_id)
        return

    book = await get_orderbook(clob_base_url, row["token_id"])
    if book is None:
        await conn.execute(
            "INSERT INTO events (level, component, message, context) VALUES ('INFO', 'paper', $1, $2)",
            "no orderbook available for simulated fill",
            json.dumps({"fill_id": fill_id, "token_id": row["token_id"]}),
        )
        return

    side = Side.BUY if row["side"] == "BUY" else Side.SELL
    sim_price, sim_shares = book.vwap_for(side, row["notional_usd"])

    context = {
        "fill_id": fill_id,
        "target": row["target"],
        "token_id": row["token_id"],
        "side": row["side"],
        "target_price": str(row["price"]),
        "target_notional_usd": str(row["notional_usd"]),
        "book_snapshot": {
            "bids": [[str(p), str(s)] for p, s in book.bids[:10]],
            "asks": [[str(p), str(s)] for p, s in book.asks[:10]],
            "taken_at": book.taken_at.isoformat(),
        },
        "sim_vwap_price": str(sim_price),
        "sim_shares_filled": str(sim_shares),
        "sim_notional_filled_usd": str(sim_price * sim_shares),
        "sim_slippage_vs_target_price": str(sim_price - row["price"]) if sim_shares > 0 else None,
    }
    await conn.execute(
        "INSERT INTO events (level, component, message, context) VALUES ('INFO', 'paper', $1, $2)",
        "simulated fill",
        json.dumps(context),
    )


async def run_paper_logger(database_url: str, clob_base_url: str) -> None:
    """LISTEN on pmex_fill (FR-W-9) and simulate each new fill as it arrives."""
    import asyncio

    listen_conn = await asyncpg.connect(database_url)
    work_conn = await asyncpg.connect(database_url)
    queue: asyncio.Queue[int] = asyncio.Queue()

    def _on_notify(_conn, _pid, _channel, payload: str) -> None:
        queue.put_nowait(int(payload))

    await listen_conn.add_listener("pmex_fill", _on_notify)
    logger.info("paper logger listening on pmex_fill")
    try:
        while True:
            fill_id = await queue.get()
            try:
                await simulate_and_log_fill(work_conn, clob_base_url, fill_id)
            except Exception:
                logger.exception("failed to simulate fill %d", fill_id)
    finally:
        await listen_conn.remove_listener("pmex_fill", _on_notify)
        await listen_conn.close()
        await work_conn.close()
