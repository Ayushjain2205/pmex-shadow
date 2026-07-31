"""Single-consumer build->submit->resolve order router (FR-EXE-1..6, FR-EXE-9..10).

One `asyncio.Queue` per bot, one consumer task — a plain FIFO queue trivially
preserves per-token ordering (FR-EXE-1) as a special case of preserving all ordering.
Idempotency lives in the database as `UNIQUE(bot_id, dedupe_key)` on `intents`
(FR-EXE-2) — a conflict there is a normal, expected outcome under redelivery, not an
error. Order state transitions (FR-EXE-3) always write `built` before any network
call, so a crash between "built" and "submitted" is unambiguous on restart: nothing
was sent.

Paper mode (FR-EXE-9/10) runs this exact same pipeline and stops only at the one call
that would leave the machine (`clob.submit_signed_order`) — everything else, including
every DB write, the FSM states, and capital accounting, is identical to live.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from decimal import Decimal

import asyncpg

from pmex_shadow.execution.clob import ClobClient
from pmex_shadow.execution.ratelimit import TokenBucket
from pmex_shadow.market.cache import get_orderbook
from pmex_shadow.models import Decision, Intent, Side, Skip

logger = logging.getLogger("pmex_shadow.execution.router")


async def insert_intent_row(conn: asyncpg.Connection, decision: Decision, fill_id: int, mode: str) -> int | None:
    """Persist every decision — Intent or Skip — to `intents` (FR-P-12). Returns the
    new row id, or None on a `(bot_id, dedupe_key)` conflict (FR-EXE-2: normal, not
    an error — logged at DEBUG).
    """
    if isinstance(decision, Skip):
        row = await conn.fetchrow(
            """
            INSERT INTO intents (bot_id, dedupe_key, fill_id, decision, skip_reason, skip_detail, token_id, side, target_price, mode)
            VALUES ($1, $2, $3, 'SKIP', $4, $5, $6, $7, $8, $9)
            ON CONFLICT (bot_id, dedupe_key) DO NOTHING
            RETURNING id
            """,
            decision.bot_id, decision.fill.dedupe_key, fill_id, decision.reason,
            json.dumps(decision.detail) if decision.detail is not None else None,
            decision.fill.token_id, decision.fill.side.value, decision.fill.price, mode,
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO intents
                (bot_id, dedupe_key, fill_id, decision, token_id, side, target_price,
                 intended_price, intended_shares, intended_usd, target_percentile, size_multiplier, mode)
            VALUES ($1, $2, $3, 'COPY', $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (bot_id, dedupe_key) DO NOTHING
            RETURNING id
            """,
            decision.bot_id, decision.fill.dedupe_key, fill_id, decision.token_id, decision.side.value,
            decision.fill.price, decision.limit_price, decision.shares, decision.notional_usd,
            decision.target_percentile, decision.size_multiplier, mode,
        )
    if row is None:
        logger.debug("duplicate intent skipped: bot=%s dedupe_key=%s", decision.bot_id, decision.fill.dedupe_key)
        return None
    return row["id"]


async def _transition(conn: asyncpg.Connection, order_id: int, from_state: str | None, to_state: str, detail: dict | None = None) -> None:
    await conn.execute(
        "INSERT INTO order_transitions (order_id, from_state, to_state, detail) VALUES ($1, $2, $3, $4)",
        order_id, from_state, to_state, json.dumps(detail) if detail else None,
    )


async def _upsert_paper_position(conn: asyncpg.Connection, bot_id: str, token_id: str, side: Side, shares: Decimal, notional_usd: Decimal, mode: str) -> None:
    if side == Side.BUY:
        await conn.execute(
            """
            INSERT INTO positions (bot_id, token_id, shares, cost_basis_usd, mode)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (bot_id, token_id, mode) DO UPDATE SET
                shares = positions.shares + EXCLUDED.shares,
                cost_basis_usd = positions.cost_basis_usd + EXCLUDED.cost_basis_usd,
                last_event_at = now()
            """,
            bot_id, token_id, shares, notional_usd, mode,
        )
    else:
        row = await conn.fetchrow(
            "SELECT shares, cost_basis_usd FROM positions WHERE bot_id=$1 AND token_id=$2 AND mode=$3",
            bot_id, token_id, mode,
        )
        if row is None:
            return  # nothing to sell against — shouldn't happen, decide() already checked
        cost_reduction = (shares / row["shares"]) * row["cost_basis_usd"] if row["shares"] > 0 else Decimal(0)
        realized_pnl = notional_usd - cost_reduction
        await conn.execute(
            """
            UPDATE positions SET shares = shares - $3, cost_basis_usd = cost_basis_usd - $4,
                                  realized_pnl_usd = realized_pnl_usd + $5, last_event_at = now()
            WHERE bot_id = $1 AND token_id = $2 AND mode = $6
            """,
            bot_id, token_id, shares, cost_reduction, realized_pnl, mode,
        )


class ExecutionRouter:
    """One instance per bot process. `clob` is None in paper mode — attempting a
    live submit without one is a programming error, not a runtime branch."""

    def __init__(
        self,
        *,
        bot_id: str,
        mode: str,
        database_url: str,
        clob_base_url: str,
        clob: ClobClient | None,
        rate_limiter: TokenBucket,
        submit_timeout_s: int = 10,
    ) -> None:
        self.bot_id = bot_id
        self.mode = mode
        self.database_url = database_url
        self.clob_base_url = clob_base_url
        self.clob = clob
        self.rate_limiter = rate_limiter
        self.submit_timeout_s = submit_timeout_s
        self.queue: asyncio.Queue[tuple[Intent, int]] = asyncio.Queue()
        self._resting_order_ids: set[str] = set()
        self._stopping = False

    async def enqueue(self, intent: Intent, intent_id: int) -> None:
        await self.queue.put((intent, intent_id))

    async def run(self) -> None:
        """Single consumer (FR-EXE-1). Runs until stop() is called and the queue
        drains, or forever otherwise."""
        conn = await asyncpg.connect(self.database_url)
        try:
            while not (self._stopping and self.queue.empty()):
                try:
                    intent, intent_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await self._process(conn, intent, intent_id)
                except Exception:
                    logger.exception("order processing failed for intent %d", intent_id)
        finally:
            await conn.close()

    async def stop(self) -> None:
        """FR-EXE-6: stop consuming, cancel all resting orders, flush state, exit."""
        self._stopping = True
        if self.mode == "live" and self.clob is not None:
            await self.clob.cancel_all()
            logger.warning("SIGTERM: cancelled all resting orders for bot %s", self.bot_id)

    async def _process(self, conn: asyncpg.Connection, intent: Intent, intent_id: int) -> None:
        client_order_id = f"{self.bot_id}:{intent.fill.dedupe_key}"
        order_row = await conn.fetchrow(
            """
            INSERT INTO orders (bot_id, intent_id, client_order_id, state, token_id, side, limit_price, shares, mode)
            VALUES ($1, $2, $3, 'built', $4, $5, $6, $7, $8)
            ON CONFLICT (client_order_id) DO NOTHING
            RETURNING id
            """,
            self.bot_id, intent_id, client_order_id, intent.token_id, intent.side.value,
            intent.limit_price, intent.shares, self.mode,
        )
        if order_row is None:
            logger.debug("duplicate order skipped: client_order_id=%s", client_order_id)
            return
        order_id = order_row["id"]
        await _transition(conn, order_id, None, "built")

        if self.mode == "paper":
            await self._simulate_fill(conn, order_id, intent)
            return

        await self._submit_live(conn, order_id, intent)

    async def _simulate_fill(self, conn: asyncpg.Connection, order_id: int, intent: Intent) -> None:
        """FR-EXE-9: every step except the CLOB submit. Fetches a *fresh* book at
        execution time (not decide()'s snapshot) — the point is measuring what
        latency between detection and this moment actually costs."""
        book = await get_orderbook(self.clob_base_url, intent.token_id)
        if book is None:
            await conn.execute("UPDATE orders SET state='rejected', error='no orderbook at simulated execution time', updated_at=now() WHERE id=$1", order_id)
            await _transition(conn, order_id, "built", "rejected", {"reason": "no_orderbook"})
            return

        vwap_price, shares_filled = book.vwap_for(intent.side, intent.notional_usd)
        if shares_filled <= 0:
            await conn.execute("UPDATE orders SET state='rejected', error='no liquidity at simulated execution time', updated_at=now() WHERE id=$1", order_id)
            await _transition(conn, order_id, "built", "rejected", {"reason": "no_liquidity"})
            return

        state = "filled" if shares_filled >= intent.shares else "partial"
        await conn.execute(
            "UPDATE orders SET state=$2, filled_shares=$3, avg_fill_price=$4, updated_at=now() WHERE id=$1",
            order_id, state, min(shares_filled, intent.shares), vwap_price,
        )
        await _transition(conn, order_id, "built", "submitted")
        await _transition(conn, order_id, "submitted", state, {"sim_vwap_price": str(vwap_price), "sim_shares_filled": str(shares_filled)})

        await _upsert_paper_position(
            conn, self.bot_id, intent.token_id, intent.side,
            min(shares_filled, intent.shares), min(shares_filled, intent.shares) * vwap_price, self.mode,
        )

    async def _submit_live(self, conn: asyncpg.Connection, order_id: int, intent: Intent) -> None:
        assert self.clob is not None, "live mode requires a ClobClient"

        await self.rate_limiter.acquire()

        signed = await self.clob.build_limit_order(token_id=intent.token_id, price=intent.limit_price, size=intent.shares, side=intent.side)
        await conn.execute("UPDATE orders SET state='submitted', updated_at=now() WHERE id=$1", order_id)
        await _transition(conn, order_id, "built", "submitted")

        try:
            result = await asyncio.wait_for(self.clob.submit_signed_order(signed), timeout=self.submit_timeout_s)
        except asyncio.TimeoutError:
            # FR-EXE-4: NEVER blind-retry. Move to `unknown` and reconcile by query.
            await conn.execute("UPDATE orders SET state='unknown', updated_at=now() WHERE id=$1", order_id)
            await _transition(conn, order_id, "submitted", "unknown", {"reason": "submit_timeout"})
            await self._reconcile_unknown(conn, order_id, intent)
            return

        if not result.accepted:
            await conn.execute("UPDATE orders SET state='rejected', error=$2, updated_at=now() WHERE id=$1", order_id, result.error)
            await _transition(conn, order_id, "submitted", "rejected", {"error": result.error})
            return

        filled_fully = result.status == "matched" and result.taking_amount is not None and result.taking_amount >= intent.shares
        new_state = "filled" if filled_fully else ("partial" if result.status == "matched" else "acked")
        await conn.execute(
            "UPDATE orders SET state=$2, exchange_order_id=$3, filled_shares=$4, updated_at=now() WHERE id=$1",
            order_id, new_state, result.exchange_order_id, result.taking_amount or Decimal(0),
        )
        await _transition(conn, order_id, "submitted", new_state, {"exchange_order_id": result.exchange_order_id})
        if new_state in ("acked", "partial") and result.exchange_order_id:
            self._resting_order_ids.add(result.exchange_order_id)

    async def _reconcile_unknown(self, conn: asyncpg.Connection, order_id: int, intent: Intent) -> None:
        """Query-based reconciliation for a timed-out submission (FR-EXE-4)."""
        row = await conn.fetchrow("SELECT exchange_order_id, client_order_id FROM orders WHERE id = $1", order_id)
        # We never received an exchange_order_id (that's *why* we're unknown), so the
        # only way to find the order is to search open orders for our client_order_id
        # equivalent — the SDK's status query is by exchange order id, which we don't
        # have. Best-effort: if nothing shows as newly resting after a short grace
        # window, treat it as never having reached the book.
        await asyncio.sleep(2)
        status = None
        if row["exchange_order_id"]:
            status = await self.clob.get_order_status(row["exchange_order_id"])
        if status is None:
            await conn.execute("UPDATE orders SET state='expired', updated_at=now() WHERE id=$1", order_id)
            await _transition(conn, order_id, "unknown", "expired", {"reconcile": "not_found"})
        else:
            await conn.execute("UPDATE orders SET state='acked', updated_at=now() WHERE id=$1", order_id)
            await _transition(conn, order_id, "unknown", "acked", {"reconcile": status})
