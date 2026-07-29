"""On-chain vs. ledger drift reconciliation (FR-L-4, FR-L-5). Runs every 60s per
bot: diffs actual on-chain positions against the ledger, advances lifecycle states
when a position becomes redeemable, and halts the bot — never trades through it —
if drift exceeds `halt_on_reconcile_drift_usd`.

Uses the SDK's `list_positions()` (backed by the Data API's indexed position view,
which already reports `redeemable`) rather than hand-rolling ERC1155 `balanceOf`
calls — it's the same on-chain truth, verified live in docs/VERIFIED.md item 5, and
avoids re-deriving what the indexer already computes correctly.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import asyncpg
from polymarket import PRODUCTION
from polymarket.clients.async_public import AsyncPublicClient

from pmex_shadow.ledger.lifecycle import ResolutionFacts, next_lifecycle

logger = logging.getLogger("pmex_shadow.ledger.reconcile")


async def reconcile_bot(
    conn: asyncpg.Connection,
    *,
    bot_id: str,
    wallet_address: str,
    mode: str,
    halt_on_drift_usd: Decimal,
) -> Decimal:
    """Returns total absolute drift found (USD). Halts the bot (writes a CRITICAL
    event; caller's next ledger read sees `halted=True` via
    ledger/subaccount.py's own events lookup) if drift exceeds the threshold.
    """
    client = AsyncPublicClient(environment=PRODUCTION)
    onchain_positions = {}
    async for page in client.list_positions(user=wallet_address):
        for p in page.items:
            onchain_positions[p.token_id] = p

    ledger_rows = await conn.fetch(
        "SELECT token_id, shares, cost_basis_usd, lifecycle, condition_id, neg_risk FROM positions "
        "WHERE bot_id = $1 AND mode = $2 AND lifecycle NOT IN ('redeemed', 'refunded', 'written_off')",
        bot_id, mode,
    )

    total_drift = Decimal(0)
    for row in ledger_rows:
        onchain = onchain_positions.get(row["token_id"])
        onchain_shares = Decimal(str(onchain.size)) if onchain else Decimal(0)
        drift_shares = abs(onchain_shares - row["shares"])
        # Value drift at cost basis (a live price would need another network call
        # per position; cost basis is the conservative floor an operator cares about
        # -- "do I actually hold what I think I paid for").
        unit_cost = (row["cost_basis_usd"] / row["shares"]) if row["shares"] > 0 else Decimal(0)
        drift_usd = drift_shares * unit_cost
        total_drift += drift_usd

        if drift_usd > 0:
            logger.warning("drift on %s/%s: ledger=%s onchain=%s (%.2f USD)", bot_id, row["token_id"], row["shares"], onchain_shares, drift_usd)

        if onchain is not None and onchain.redeemable:
            facts = ResolutionFacts(resolved=True, disputed=False, voided=False, winning=True)
            new_state = next_lifecycle(row["lifecycle"], facts)
            if new_state != row["lifecycle"]:
                await conn.execute(
                    "UPDATE positions SET lifecycle = $3, condition_id = COALESCE(condition_id, $4), "
                    "neg_risk = $5, last_event_at = now() WHERE bot_id = $1 AND token_id = $2",
                    bot_id, row["token_id"], new_state, onchain.condition_id, onchain.negative_risk,
                )
                await conn.execute(
                    "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'INFO', 'ledger.reconcile', 'lifecycle advanced', $2)",
                    bot_id, f'{{"token_id": "{row["token_id"]}", "from": "{row["lifecycle"]}", "to": "{new_state}"}}',
                )

    if total_drift > halt_on_drift_usd:
        await conn.execute(
            "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'CRITICAL', 'ledger.reconcile', 'drift exceeds halt threshold, halting bot', $2)",
            bot_id, f'{{"total_drift_usd": "{total_drift}", "threshold_usd": "{halt_on_drift_usd}"}}',
        )
        logger.critical("bot %s halted: drift %.2f exceeds threshold %.2f", bot_id, total_drift, halt_on_drift_usd)

    return total_drift


async def run_reconcile_loop(database_url: str, bot_id: str, wallet_address: str, mode: str, halt_on_drift_usd: Decimal, interval_s: int = 60) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        while True:
            try:
                await reconcile_bot(conn, bot_id=bot_id, wallet_address=wallet_address, mode=mode, halt_on_drift_usd=halt_on_drift_usd)
            except Exception:
                logger.exception("reconcile pass failed for bot %s", bot_id)
            await asyncio.sleep(interval_s)
    finally:
        await conn.close()
