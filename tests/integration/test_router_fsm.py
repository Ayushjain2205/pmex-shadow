"""Integration tests for the order FSM (FR-EXE-3, FR-EXE-4) — real Postgres, per
§10's testing philosophy ("full pipeline against a local Postgres with a mocked
CLOB"), fake CLOB client so no network/funds are involved. Requires
PMEX_DATABASE_URL to point at a real, migrated Postgres — run inside the docker
compose stack, not part of the default unit test run.
"""

import asyncio
import datetime as dt
import os
from decimal import Decimal

import asyncpg
import pytest

from pmex_shadow.execution.clob import OrderResult
from pmex_shadow.execution.ratelimit import TokenBucket
from pmex_shadow.execution.router import ExecutionRouter, insert_intent_row
from pmex_shadow.models import Intent, Side, TargetFill

pytestmark = pytest.mark.skipif(
    "PMEX_TEST_DATABASE_URL" not in os.environ, reason="requires PMEX_TEST_DATABASE_URL (real Postgres)"
)

DATABASE_URL = os.environ.get("PMEX_TEST_DATABASE_URL", "")


class FakeClob:
    def __init__(self, *, accept: bool = True, hang: bool = False, exchange_order_id: str = "fake-order-1"):
        self.accept = accept
        self.hang = hang
        self.exchange_order_id = exchange_order_id
        self.cancel_all_called = False
        self.built = []
        self.submitted = []

    async def build_limit_order(self, *, token_id, price, size, side):
        self.built.append((token_id, price, size, side))
        return {"token_id": token_id, "price": price, "size": size, "side": side}

    async def submit_signed_order(self, signed):
        self.submitted.append(signed)
        if self.hang:
            await asyncio.sleep(30)
        if not self.accept:
            return OrderResult(accepted=False, exchange_order_id=None, status=None, making_amount=None, taking_amount=None, error="42: insufficient balance")
        return OrderResult(
            accepted=True, exchange_order_id=self.exchange_order_id, status="live",
            making_amount=signed["size"] * signed["price"], taking_amount=Decimal(0), error=None,
        )

    async def cancel_all(self):
        self.cancel_all_called = True
        return True

    async def get_order_status(self, exchange_order_id):
        return None  # simulate "not found" for the unknown->reconcile path


async def _fresh_fill(conn) -> tuple[TargetFill, int]:
    dedupe_key = f"test:{dt.datetime.now().timestamp()}"
    row = await conn.fetchrow(
        """
        INSERT INTO target_fills (dedupe_key, target, token_id, side, price, size, notional_usd, block_ts, detected_at, source, raw)
        VALUES ($1, '0xtest', 'tok1', 'BUY', 0.5, 10, 5, now(), now(), 'chain', '{}')
        RETURNING id
        """,
        dedupe_key,
    )
    fill = TargetFill(
        dedupe_key=dedupe_key, target="0xtest", token_id="tok1", side=Side.BUY, price=Decimal("0.5"),
        size=Decimal("10"), notional_usd=Decimal("5"), block_number=1, block_ts=dt.datetime.now(dt.timezone.utc),
        detected_at=dt.datetime.now(dt.timezone.utc), source="chain",
    )
    return fill, row["id"]


def _intent_for(fill: TargetFill) -> Intent:
    return Intent(
        bot_id="testbot", fill=fill, token_id=fill.token_id, side=Side.BUY,
        limit_price=Decimal("0.51"), shares=Decimal("10"), notional_usd=Decimal("5.1"),
        target_percentile=Decimal("80"), size_multiplier=Decimal("1.5"),
    )


@pytest.fixture
async def conn():
    c = await asyncpg.connect(DATABASE_URL)
    yield c
    await c.close()


async def test_accepted_order_reaches_acked(conn):
    fill, fill_id = await _fresh_fill(conn)
    intent = _intent_for(fill)
    intent_id = await insert_intent_row(conn, intent, fill_id, "live")

    clob = FakeClob(accept=True)
    router = ExecutionRouter(bot_id="testbot", mode="live", database_url=DATABASE_URL, clob_base_url="http://x", clob=clob, rate_limiter=TokenBucket(600))
    await router._process(conn, intent, intent_id)

    row = await conn.fetchrow("SELECT state, exchange_order_id FROM orders WHERE intent_id = $1", intent_id)
    assert row["state"] == "acked"
    assert row["exchange_order_id"] == "fake-order-1"

    transitions = await conn.fetch(
        "SELECT from_state, to_state FROM order_transitions ot JOIN orders o ON o.id = ot.order_id WHERE o.intent_id = $1 ORDER BY ot.id",
        intent_id,
    )
    states = [(t["from_state"], t["to_state"]) for t in transitions]
    assert states == [(None, "built"), ("built", "submitted"), ("submitted", "acked")]


async def test_rejected_order_reaches_rejected_state(conn):
    fill, fill_id = await _fresh_fill(conn)
    intent = _intent_for(fill)
    intent_id = await insert_intent_row(conn, intent, fill_id, "live")

    clob = FakeClob(accept=False)
    router = ExecutionRouter(bot_id="testbot", mode="live", database_url=DATABASE_URL, clob_base_url="http://x", clob=clob, rate_limiter=TokenBucket(600))
    await router._process(conn, intent, intent_id)

    row = await conn.fetchrow("SELECT state, error FROM orders WHERE intent_id = $1", intent_id)
    assert row["state"] == "rejected"
    assert "insufficient balance" in row["error"]


async def test_timeout_reaches_unknown_then_reconciles_never_retries(conn):
    fill, fill_id = await _fresh_fill(conn)
    intent = _intent_for(fill)
    intent_id = await insert_intent_row(conn, intent, fill_id, "live")

    clob = FakeClob(hang=True)
    router = ExecutionRouter(bot_id="testbot", mode="live", database_url=DATABASE_URL, clob_base_url="http://x", clob=clob, rate_limiter=TokenBucket(600), submit_timeout_s=1)
    await router._process(conn, intent, intent_id)

    row = await conn.fetchrow("SELECT state FROM orders WHERE intent_id = $1", intent_id)
    # never_found -> expired (the fake's get_order_status always returns None)
    assert row["state"] == "expired"
    # exactly one submit call -- never blind-retried (FR-EXE-4)
    assert len(clob.submitted) == 1

    transitions = await conn.fetch(
        "SELECT to_state FROM order_transitions ot JOIN orders o ON o.id = ot.order_id WHERE o.intent_id = $1 ORDER BY ot.id",
        intent_id,
    )
    to_states = [t["to_state"] for t in transitions]
    assert "unknown" in to_states


async def test_duplicate_intent_produces_exactly_one_order(conn):
    fill, fill_id = await _fresh_fill(conn)
    intent = _intent_for(fill)
    intent_id_1 = await insert_intent_row(conn, intent, fill_id, "live")
    intent_id_2 = await insert_intent_row(conn, intent, fill_id, "live")  # redelivery of the same fill

    assert intent_id_1 is not None
    assert intent_id_2 is None  # FR-EXE-2: conflict, not an error

    clob = FakeClob(accept=True)
    router = ExecutionRouter(bot_id="testbot", mode="live", database_url=DATABASE_URL, clob_base_url="http://x", clob=clob, rate_limiter=TokenBucket(600))
    await router._process(conn, intent, intent_id_1)
    await router._process(conn, intent, intent_id_1)  # same intent processed twice

    count = await conn.fetchval("SELECT count(*) FROM orders WHERE intent_id = $1", intent_id_1)
    assert count == 1


async def test_sigterm_cancels_all_resting_orders(conn):
    clob = FakeClob(accept=True)
    router = ExecutionRouter(bot_id="testbot", mode="live", database_url=DATABASE_URL, clob_base_url="http://x", clob=clob, rate_limiter=TokenBucket(600))
    await router.stop()
    assert clob.cancel_all_called
