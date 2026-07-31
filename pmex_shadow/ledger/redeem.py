"""Scheduled redemption job (FR-L-6..10), not the hot path — runs hourly. Routes
through the SDK's single `redeem_positions(condition_id=...)` method rather than
manually choosing between the plain-CTF and NegRisk-router call builders — the SDK
already handles that branch internally (see docs/VERIFIED.md item 9's resolution),
so replicating FR-L-7's routing logic by hand here would just be a second,
independently-maintained copy of a decision the SDK already makes correctly.

Not batched yet (FR-L-10 asks for it where the contract permits) — each position
redeems as its own transaction. Noted as a scope cut, not an oversight: correctness
first, since this is real money and real gas either way.

`redeem_bot_positions` below is live-mode only — it was never started for paper bots
at all (cli.py only created this loop inside `if mode == "live":"), so paper
positions never closed out on resolution, silently violating FR-EXE-10 ("credits the
paper ledger on simulated resolution") until this was actually run against real
target wallets for long enough to hit `max_concurrent_positions` and watch it block
every subsequent trade. `credit_resolved_paper_positions` (bottom of this file) is
the fix — a parallel path for paper, not a rewrite of the live path, since paper
positions have no real on-chain holdings to check the same way (see
`ResolutionStatus`'s docstring in models.py).
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import asyncpg
import httpx
from polymarket import PRODUCTION
from polymarket.clients.async_public import AsyncPublicClient
from polymarket.clients.async_secure import AsyncSecureClient

from pmex_shadow.ledger.lifecycle import ResolutionFacts, redeem_retry_allowed, should_write_off

logger = logging.getLogger("pmex_shadow.ledger.redeem")

MIN_POL_FOR_REDEMPTION = Decimal("0.05")  # conservative floor; a single redemption is reportedly ~0.02 POL (VERIFIED.md item 10, third-party estimate)


async def get_pol_balance(rpc_http_url: str, address: str) -> Decimal:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(rpc_http_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]})
    resp.raise_for_status()
    wei = int(resp.json()["result"], 16)
    return Decimal(wei) / Decimal(10**18)


async def redeem_bot_positions(
    conn: asyncpg.Connection,
    *,
    bot_id: str,
    wallet_address: str,
    mode: str,
    rpc_http_url: str,
    redeem_retry_days: int,
    secure_client: AsyncSecureClient | None,
) -> None:
    """`secure_client` is None in paper mode — nothing to redeem for real, this is a
    no-op there. In live mode it must be an authenticated client for `wallet_address`.
    """
    rows = await conn.fetch(
        "SELECT id, token_id, condition_id, lifecycle, opened_at FROM positions "
        "WHERE bot_id = $1 AND mode = $2 AND lifecycle IN ('open', 'pending_resolution', 'resolved', 'disputed') "
        "AND condition_id IS NOT NULL",
        bot_id, mode,
    )
    if not rows:
        return

    public_client = AsyncPublicClient(environment=PRODUCTION)
    fresh_positions = {}
    async for page in public_client.list_positions(user=wallet_address):
        for p in page.items:
            fresh_positions[p.token_id] = p

    for row in rows:
        onchain = fresh_positions.get(row["token_id"])
        if onchain is None:
            continue  # nothing on-chain for this token anymore (already redeemed elsewhere, or never settled)

        facts = ResolutionFacts(
            resolved=bool(onchain.redeemable) or (onchain.cur_price in (0, 1)),
            disputed=False,  # the Data API view doesn't surface dispute status directly; treated conservatively as "not yet confirmed disputed" rather than guessed
            voided=False,
            winning=bool(onchain.cur_price == 1) if onchain.cur_price in (0, 1) else None,
        )

        if should_write_off(facts):
            await conn.execute(
                "UPDATE positions SET lifecycle = 'written_off', last_event_at = now() WHERE id = $1", row["id"],
            )
            await conn.execute(
                "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'INFO', 'ledger.redeem', 'losing position written off, no transaction', $2)",
                bot_id, f'{{"token_id": "{row["token_id"]}", "condition_id": "{row["condition_id"]}"}}',
            )
            continue

        if not onchain.redeemable:
            continue  # resolved-in-UI is not the same as redeemable on-chain (FR-L-6) -- wait for the real signal

        if row["lifecycle"] == "disputed":
            dispute_age_days = 0  # placeholder until dispute timestamps are tracked (needs UMA adapter event data, not yet wired)
            if not redeem_retry_allowed(dispute_age_days, redeem_retry_days):
                continue

        if secure_client is None:
            logger.debug("paper mode: skipping real redemption for %s", row["token_id"])
            continue

        pol_balance = await get_pol_balance(rpc_http_url, wallet_address)
        if pol_balance < MIN_POL_FOR_REDEMPTION:
            await conn.execute(
                "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'CRITICAL', 'ledger.redeem', 'insufficient POL for redemption', $2)",
                bot_id, f'{{"pol_balance": "{pol_balance}", "required": "{MIN_POL_FOR_REDEMPTION}"}}',
            )
            logger.critical("bot %s: insufficient POL (%.4f) to redeem %s", bot_id, pol_balance, row["token_id"])
            continue

        try:
            handle = await secure_client.redeem_positions(condition_id=row["condition_id"])
            outcome = await handle.wait()
        except Exception as exc:
            logger.exception("redemption failed for %s/%s", bot_id, row["token_id"])
            await conn.execute(
                "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'CRITICAL', 'ledger.redeem', 'redemption transaction failed', $2)",
                bot_id, f'{{"token_id": "{row["token_id"]}", "error": "{str(exc)[:200]}"}}',
            )
            continue

        await conn.execute(
            "UPDATE positions SET lifecycle = 'redeemed', last_event_at = now() WHERE id = $1", row["id"],
        )
        await conn.execute(
            "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'INFO', 'ledger.redeem', 'redeemed', $2)",
            bot_id, f'{{"token_id": "{row["token_id"]}", "tx_hash": "{outcome.transaction_hash}"}}',
        )


async def run_redeem_loop(
    database_url: str, bot_id: str, wallet_address: str, mode: str, rpc_http_url: str,
    redeem_retry_days: int, secure_client: AsyncSecureClient | None, interval_s: int = 3600,
) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        while True:
            try:
                await redeem_bot_positions(
                    conn, bot_id=bot_id, wallet_address=wallet_address, mode=mode, rpc_http_url=rpc_http_url,
                    redeem_retry_days=redeem_retry_days, secure_client=secure_client,
                )
            except Exception:
                logger.exception("redeem pass failed for bot %s", bot_id)
            await asyncio.sleep(interval_s)
    finally:
        await conn.close()


async def credit_resolved_paper_positions(conn: asyncpg.Connection, *, bot_id: str, gamma_api_base_url: str) -> None:
    """Paper-mode counterpart to `redeem_bot_positions` (FR-EXE-10). A paper trade
    never touches a real wallet — positions are synthetic bookkeeping keyed by
    (bot_id, token_id, mode) — so there's no on-chain state to check the way live
    mode does. Resolution comes from Gamma's closed/outcomePrices view instead
    (`market.cache.get_resolution_status`). No transaction, ever: "credit" means
    updating the row directly the moment the market shows closed, same instant
    whether the outcome won or lost — there's no gas cost to wait out either way.
    """
    from pmex_shadow.market.cache import get_resolution_status

    rows = await conn.fetch(
        "SELECT id, token_id, shares, cost_basis_usd FROM positions WHERE bot_id = $1 AND mode = 'paper' AND lifecycle = 'open'",
        bot_id,
    )
    for row in rows:
        status = await get_resolution_status(gamma_api_base_url, row["token_id"])
        if status is None or not status.closed:
            continue

        facts = ResolutionFacts(resolved=True, disputed=False, voided=False, winning=status.won)

        if should_write_off(facts):
            realized = -row["cost_basis_usd"]
            await conn.execute(
                "UPDATE positions SET shares = 0, cost_basis_usd = 0, realized_pnl_usd = realized_pnl_usd + $2, "
                "lifecycle = 'written_off', last_event_at = now() WHERE id = $1",
                row["id"], realized,
            )
            await conn.execute(
                "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'INFO', 'ledger.redeem', 'losing position written off, no transaction', $2)",
                bot_id, f'{{"token_id": "{row["token_id"]}", "realized_pnl_usd": "{realized}"}}',
            )
            continue

        payout = row["shares"]  # $1/winning share, guaranteed by the CTF payout mechanics
        realized = payout - row["cost_basis_usd"]
        await conn.execute(
            "UPDATE positions SET shares = 0, cost_basis_usd = 0, realized_pnl_usd = realized_pnl_usd + $2, "
            "lifecycle = 'redeemed', last_event_at = now() WHERE id = $1",
            row["id"], realized,
        )
        await conn.execute(
            "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'INFO', 'ledger.redeem', 'paper resolution credited, no real redemption', $2)",
            bot_id, f'{{"token_id": "{row["token_id"]}", "realized_pnl_usd": "{realized}"}}',
        )


async def run_paper_resolution_loop(database_url: str, bot_id: str, gamma_api_base_url: str, interval_s: int = 60) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        while True:
            try:
                await credit_resolved_paper_positions(conn, bot_id=bot_id, gamma_api_base_url=gamma_api_base_url)
            except Exception:
                logger.exception("paper resolution pass failed for bot %s", bot_id)
            await asyncio.sleep(interval_s)
    finally:
        await conn.close()
