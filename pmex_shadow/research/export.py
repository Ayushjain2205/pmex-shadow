"""CSV export of fills and realized PnL — users will need this for taxes (design
doc §3.7). Plain Postgres reads, same as everything else financial (FR-C-1).
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import asyncpg


async def export_fills_csv(conn: asyncpg.Connection, since: dt.datetime) -> str:
    rows = await conn.fetch(
        """
        SELECT i.bot_id, i.decision, i.skip_reason, i.token_id, i.side, i.target_price,
               i.intended_price, i.intended_shares, i.intended_usd, i.mode, i.created_at,
               o.state AS order_state, o.filled_shares, o.avg_fill_price
        FROM intents i
        LEFT JOIN orders o ON o.intent_id = i.id
        WHERE i.created_at >= $1
        ORDER BY i.created_at
        """,
        since,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "bot_id", "decision", "skip_reason", "token_id", "side", "target_price",
        "intended_price", "intended_shares", "intended_usd", "mode", "created_at",
        "order_state", "filled_shares", "avg_fill_price",
    ])
    for r in rows:
        writer.writerow([r[k] for k in (
            "bot_id", "decision", "skip_reason", "token_id", "side", "target_price",
            "intended_price", "intended_shares", "intended_usd", "mode", "created_at",
            "order_state", "filled_shares", "avg_fill_price",
        )])
    return buf.getvalue()


async def export_pnl_csv(conn: asyncpg.Connection, since: dt.datetime) -> str:
    rows = await conn.fetch(
        """
        SELECT bot_id, token_id, shares, cost_basis_usd, realized_pnl_usd, lifecycle,
               condition_id, neg_risk, opened_at, last_event_at, mode
        FROM positions
        WHERE last_event_at >= $1
        ORDER BY last_event_at
        """,
        since,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "bot_id", "token_id", "shares", "cost_basis_usd", "realized_pnl_usd", "lifecycle",
        "condition_id", "neg_risk", "opened_at", "last_event_at", "mode",
    ])
    for r in rows:
        writer.writerow([r[k] for k in (
            "bot_id", "token_id", "shares", "cost_basis_usd", "realized_pnl_usd", "lifecycle",
            "condition_id", "neg_risk", "opened_at", "last_event_at", "mode",
        )])
    return buf.getvalue()
