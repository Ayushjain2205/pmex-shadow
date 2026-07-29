"""Per-(bot_id, token_id, mode) position bookkeeping (FR-L-1). The read side ships
now — the execution router (Phase 3) needs `get_ledger_state()` to build `decide()`'s
`LedgerState` input. Write-side lifecycle transitions, reconciliation, and redemption
are the rest of Phase 4.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from pmex_shadow.models import LedgerState, Position


async def get_ledger_state(conn: asyncpg.Connection, bot_id: str, mode: str) -> LedgerState:
    rows = await conn.fetch(
        "SELECT token_id, shares, cost_basis_usd FROM positions WHERE bot_id = $1 AND mode = $2 AND lifecycle = 'open'",
        bot_id, mode,
    )
    positions = tuple(
        Position(token_id=r["token_id"], shares=r["shares"], cost_basis_usd=r["cost_basis_usd"]) for r in rows
    )
    deployed_usd = sum((p.cost_basis_usd for p in positions), Decimal(0))

    global_row = await conn.fetchrow(
        "SELECT COALESCE(sum(cost_basis_usd), 0) AS total FROM positions WHERE mode = $1 AND lifecycle = 'open'",
        mode,
    )
    global_exposure_usd = global_row["total"]

    halted_row = await conn.fetchrow(
        "SELECT 1 FROM events WHERE bot_id = $1 AND level = 'CRITICAL' AND component = 'ledger.reconcile' "
        "AND at > now() - interval '1 day' ORDER BY id DESC LIMIT 1",
        bot_id,
    )
    # Halts are sticky only via an explicit resume (ledger/reconcile.py, Phase 4) —
    # this 24h lookback is a placeholder until that command exists, so a halt doesn't
    # silently expire on its own overnight.
    halted = halted_row is not None

    return LedgerState(positions=positions, deployed_usd=deployed_usd, global_exposure_usd=global_exposure_usd, halted=halted)
