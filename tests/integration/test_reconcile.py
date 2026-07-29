"""Integration test for reconcile_bot() against REAL on-chain position data (a known
wallet with real redeemable positions, found live while building this) and a real
Postgres. No funds move — this only reads positions and writes to our own DB.
"""

import os
from decimal import Decimal

import asyncpg
import pytest

from pmex_shadow.ledger.reconcile import reconcile_bot

pytestmark = pytest.mark.skipif(
    "PMEX_TEST_DATABASE_URL" not in os.environ, reason="requires PMEX_TEST_DATABASE_URL (real Postgres) and live network"
)

DATABASE_URL = os.environ.get("PMEX_TEST_DATABASE_URL", "")

# A real wallet confirmed (2026-07-29) to hold real redeemable positions via
# list_positions() while building this feature. This is inherently time-sensitive --
# the wallet may redeem or its positions may change by the time this runs later. If
# test_reconcile_advances_lifecycle_for_real_redeemable_position starts failing,
# re-run the discovery query against a currently-active wallet rather than assuming
# the code regressed:
#   list_positions(user=<addr>) and pick any item with redeemable=True.
REAL_WALLET_WITH_REDEEMABLE_POSITIONS = "0x1230e394bdb4e28f67dc8b37996f1c28ec8edd03"
REAL_REDEEMABLE_TOKEN_ID = "25677949463852645916751775389806771475374975319799598266477739890379934389944"


@pytest.fixture
async def conn():
    c = await asyncpg.connect(DATABASE_URL)
    yield c
    await c.close()


async def test_reconcile_advances_lifecycle_for_real_redeemable_position(conn):
    bot_id = "reconcile_test_bot"
    await conn.execute("DELETE FROM positions WHERE bot_id = $1", bot_id)
    await conn.execute(
        """
        INSERT INTO positions (bot_id, token_id, shares, cost_basis_usd, lifecycle, mode)
        VALUES ($1, $2, 100, 50, 'open', 'live')
        """,
        bot_id, REAL_REDEEMABLE_TOKEN_ID,
    )

    await reconcile_bot(
        conn, bot_id=bot_id, wallet_address=REAL_WALLET_WITH_REDEEMABLE_POSITIONS,
        mode="live", halt_on_drift_usd=Decimal("1000000"),  # high threshold: this test is about lifecycle, not drift-halting
    )

    row = await conn.fetchrow("SELECT lifecycle, condition_id FROM positions WHERE bot_id = $1 AND token_id = $2", bot_id, REAL_REDEEMABLE_TOKEN_ID)
    assert row["lifecycle"] == "resolved"
    assert row["condition_id"] is not None

    event = await conn.fetchrow(
        "SELECT message FROM events WHERE bot_id = $1 AND component = 'ledger.reconcile' ORDER BY id DESC LIMIT 1", bot_id,
    )
    assert event["message"] == "lifecycle advanced"

    await conn.execute("DELETE FROM positions WHERE bot_id = $1", bot_id)
    await conn.execute("DELETE FROM events WHERE bot_id = $1", bot_id)


async def test_reconcile_halts_bot_on_large_drift(conn):
    bot_id = "reconcile_drift_test_bot"
    await conn.execute("DELETE FROM positions WHERE bot_id = $1", bot_id)
    # A position we claim to hold 100,000 shares of, on a token this wallet doesn't
    # actually hold at all -- guaranteed large drift.
    await conn.execute(
        """
        INSERT INTO positions (bot_id, token_id, shares, cost_basis_usd, lifecycle, mode)
        VALUES ($1, 'nonexistent-token-id', 100000, 50000, 'open', 'live')
        """,
        bot_id,
    )

    await reconcile_bot(
        conn, bot_id=bot_id, wallet_address=REAL_WALLET_WITH_REDEEMABLE_POSITIONS,
        mode="live", halt_on_drift_usd=Decimal("100"),
    )

    event = await conn.fetchrow(
        "SELECT level, message FROM events WHERE bot_id = $1 AND component = 'ledger.reconcile' ORDER BY id DESC LIMIT 1", bot_id,
    )
    assert event["level"] == "CRITICAL"
    assert "halt" in event["message"]

    await conn.execute("DELETE FROM positions WHERE bot_id = $1", bot_id)
    await conn.execute("DELETE FROM events WHERE bot_id = $1", bot_id)
