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

    realized_row = await conn.fetchrow(
        "SELECT COALESCE(sum(realized_pnl_usd), 0) AS total FROM positions WHERE bot_id = $1 AND mode = $2",
        bot_id, mode,
    )
    realized_pnl_usd = realized_row["total"]

    # Halted if the most recent halt-type event (reconcile drift, FR-L-5, or an
    # explicit killswitch, FR-O-3) is more recent than the most recent resume — never
    # a time-window heuristic. A halt is sticky until `pmex-shadow bots resume`
    # explicitly clears it (ops/killswitch.py), not until some interval elapses.
    last_halt = await conn.fetchrow(
        "SELECT at FROM events WHERE bot_id = $1 AND level = 'CRITICAL' "
        "AND component IN ('ledger.reconcile', 'killswitch') ORDER BY at DESC LIMIT 1",
        bot_id,
    )
    last_resume = await conn.fetchrow(
        "SELECT at FROM events WHERE bot_id = $1 AND component = 'killswitch' AND message = 'resumed' ORDER BY at DESC LIMIT 1",
        bot_id,
    )
    halted = last_halt is not None and (last_resume is None or last_halt["at"] > last_resume["at"])

    return LedgerState(
        positions=positions, deployed_usd=deployed_usd, global_exposure_usd=global_exposure_usd,
        halted=halted, realized_pnl_usd=realized_pnl_usd,
    )
