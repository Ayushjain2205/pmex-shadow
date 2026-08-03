"""The catch-up poll's block-range arithmetic and failure behaviour.

Stubbed rather than hitting a provider: what matters here is which ranges get
scanned and whether the loop survives an error, both of which are pure logic. The
end-to-end property (fills actually landing within seconds) is only observable
against a live chain and is verified by watching detect-lag bands after deploy.
"""

from __future__ import annotations

import asyncio

import pytest

from pmex_shadow.watcher import chain


class FakeRPC:
    """Stands in for _rpc, recording the eth_getLogs ranges requested."""

    def __init__(self, heads):
        self.heads = list(heads)
        self.ranges: list[tuple[int, int]] = []
        self.fail_next = 0

    async def __call__(self, client, http_url, method, params):
        if method == "eth_blockNumber":
            if self.fail_next > 0:
                self.fail_next -= 1
                raise RuntimeError("provider blew up")
            return hex(self.heads.pop(0) if self.heads else 0)
        if method == "eth_getLogs":
            self.ranges.append((int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)))
            return []
        raise AssertionError(f"unexpected rpc {method}")


class FakeConn:
    def __init__(self, cursor):
        self.cursor = cursor

    async def fetchrow(self, *a):
        return None if self.cursor is None else {"last_processed_block": self.cursor}

    async def execute(self, sql, *args):
        # Mirrors the GREATEST in set_cursor: coverage never rewinds.
        if "watcher_cursor" in sql:
            self.cursor = max(self.cursor or 0, args[0])


async def run_ticks(rpc, conn, n, interval_s=0.001, targets={"0xabc"}):
    """Run the loop for n ticks, then cancel it the way _subscribe_once does."""
    chain._rpc = rpc
    task = asyncio.create_task(
        chain._catch_up_loop(None, "http://rpc", conn, targets, chunk_blocks=2000, interval_s=interval_s)
    )
    await asyncio.sleep(interval_s * n + 0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.fixture(autouse=True)
def restore_rpc():
    original = chain._rpc
    yield
    chain._rpc = original


async def test_scans_from_the_block_after_the_cursor():
    """The cursor is inclusive-covered. Re-scanning it every tick would be harmless
    but wasteful at this cadence."""
    rpc = FakeRPC(heads=[105])
    await run_ticks(rpc, FakeConn(cursor=100), n=1)
    assert rpc.ranges == [(101, 105)]


async def test_advances_the_cursor_so_ranges_do_not_overlap():
    """The whole point: coverage tracks the chain, so each tick's range starts where
    the last one ended and a disconnect never leaves a large gap to backfill."""
    conn = FakeConn(cursor=100)
    rpc = FakeRPC(heads=[102, 104, 107])
    await run_ticks(rpc, conn, n=3)
    assert rpc.ranges == [(101, 102), (103, 104), (105, 107)]
    assert conn.cursor == 107


async def test_no_work_when_the_cursor_is_already_at_head():
    """A quiet chain must not produce eth_getLogs calls — this loop runs every couple
    of seconds forever."""
    rpc = FakeRPC(heads=[100, 100, 100])
    await run_ticks(rpc, FakeConn(cursor=100), n=3)
    assert rpc.ranges == []


async def test_first_run_with_no_cursor_starts_at_head():
    """Never backfill the entire chain from genesis on a fresh install."""
    conn = FakeConn(cursor=None)
    rpc = FakeRPC(heads=[500, 500])
    await run_ticks(rpc, conn, n=2)
    assert rpc.ranges == []
    assert conn.cursor == 500


async def test_survives_a_transient_rpc_failure():
    """A provider hiccup must not silently end coverage — this loop is the only path
    when the subscription is dead, and it dying would be invisible."""
    conn = FakeConn(cursor=100)
    rpc = FakeRPC(heads=[103])
    rpc.fail_next = 1
    await run_ticks(rpc, conn, n=3)
    assert rpc.ranges == [(101, 103)], "loop stopped after the error instead of retrying"


async def test_cursor_never_rewinds():
    """set_cursor uses GREATEST because the reconnect backfill and this loop can both
    write around a reconnect; a late writer must not re-open covered ground."""
    conn = FakeConn(cursor=200)
    await chain.set_cursor(conn, 150)
    assert conn.cursor == 200
    await chain.set_cursor(conn, 250)
    assert conn.cursor == 250
