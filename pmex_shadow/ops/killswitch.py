"""Global and per-bot killswitch (FR-O-3). Halting writes a CRITICAL `events` row
that `ledger/subaccount.py`'s `get_ledger_state()` already checks — the same
mechanism the reconciler uses to halt on drift (FR-L-5), not a second parallel
concept of "halted."

`--flatten` cancels resting orders only — it deliberately does NOT auto-liquidate
open positions with market sells. Automatically selling real assets at whatever
price is available is a materially different, more dangerous action than cancelling
unfilled orders, and building + safely testing that without ever exercising it
against real funds isn't something to ship confidently under this scope. Flattening
positions stays an operator decision, made with eyes on the book.
"""

from __future__ import annotations

import json

import asyncpg


async def halt_bot(conn: asyncpg.Connection, bot_id: str, reason: str, flatten: bool) -> None:
    await conn.execute(
        "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'CRITICAL', 'killswitch', 'halted', $2)",
        bot_id, json.dumps({"reason": reason, "flatten_requested": flatten}),
    )


async def halt_all(conn: asyncpg.Connection, reason: str, flatten: bool) -> list[str]:
    rows = await conn.fetch("SELECT DISTINCT bot_id FROM bot_config WHERE active")
    bot_ids = [r["bot_id"] for r in rows]
    for bot_id in bot_ids:
        await halt_bot(conn, bot_id, reason, flatten)
    return bot_ids


async def resume_bot(conn: asyncpg.Connection, bot_id: str) -> None:
    """Explicit operator action — nothing in this system clears a halt on its own,
    time-based or otherwise (see ledger/subaccount.py's get_ledger_state)."""
    await conn.execute(
        "INSERT INTO events (bot_id, level, component, message) VALUES ($1, 'INFO', 'killswitch', 'resumed')", bot_id,
    )


async def cancel_resting_orders(bot_id: str, wallet_env: str, pk_env: str, api_key_prefix: str, clob_base_url: str) -> bool:
    """Best-effort: only works if this bot's live credentials are present in the
    environment (i.e. run from the same context bot_run would use). Returns False,
    not an exception, if credentials aren't available — a killswitch that crashes
    because a paper-mode bot has no CLOB creds would be worse than one that no-ops.
    """
    import os

    from pmex_shadow.execution.clob import ClobClient

    pk = os.environ.get(pk_env)
    funder = os.environ.get(wallet_env)
    api_key = os.environ.get(f"{api_key_prefix}_API_KEY")
    api_secret = os.environ.get(f"{api_key_prefix}_API_SECRET")
    api_passphrase = os.environ.get(f"{api_key_prefix}_API_PASSPHRASE")
    if not all([pk, funder, api_key, api_secret, api_passphrase]):
        return False

    client = await ClobClient.create(private_key=pk, wallet=funder, api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    try:
        await client.cancel_all()
        return True
    finally:
        await client.close()
