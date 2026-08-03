"""The learned eth_getLogs range cap.

The provider's real cap isn't discoverable from an API, so backfill() probes for it
by halving until the rejections stop. These cover what has to be true of that probe:
it is paid once per process, and it only fires for the error it is meant to fix.
"""

from __future__ import annotations

import httpx
import pytest

from pmex_shadow.watcher import chain


class RangeCappedRPC:
    """Stands in for _rpc as a provider that rejects any range wider than `cap`."""

    def __init__(self, cap: int, error: Exception | None = None):
        self.cap = cap
        self.error = error
        self.ranges: list[tuple[int, int]] = []
        self.rejections = 0

    async def __call__(self, client, http_url, method, params):
        if method != "eth_getLogs":
            raise AssertionError(f"unexpected rpc {method}")
        lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        if hi - lo + 1 > self.cap:
            self.rejections += 1
            raise self.error or RuntimeError(
                "eth_getLogs failed: {'code': -32000, 'message': 'invalid block range params'}"
            )
        self.ranges.append((lo, hi))
        return []


class FakeConn:
    def __init__(self):
        self.cursor = None

    async def execute(self, sql, *args):
        if "watcher_cursor" in sql:
            self.cursor = max(self.cursor or 0, args[0])


@pytest.fixture(autouse=True)
def restore_rpc():
    original = chain._rpc
    yield
    chain._rpc = original


async def run_backfill(rpc, cap, from_block, to_block):
    chain._rpc = rpc
    await chain.backfill(None, "http://rpc", FakeConn(), {"0xabc"}, from_block, to_block, cap)


@pytest.mark.asyncio
async def test_probe_is_paid_once_not_once_per_backfill():
    """The regression: `chunk` used to be a local re-seeded from chunk_blocks on every
    call, so each backfill re-walked 2000 -> ... -> 10 and burned 8 rejections before
    its first useful request — on the recovery path that is already behind."""
    rpc = RangeCappedRPC(cap=10)
    cap = chain.RangeCap(2000)

    # Wider than the provider's cap, so the probe actually fires — `end` is clamped to
    # to_block, so a range already under the cap never rejects and never teaches
    # anything.
    await run_backfill(rpc, cap, 1000, 1100)
    after_first = rpc.rejections
    assert after_first == 8, "2000 -> 1000 -> 500 -> 250 -> 125 -> 62 -> 31 -> 15 -> 10"
    assert cap.value == 10

    for _ in range(5):
        await run_backfill(rpc, cap, 2000, 2100)
    assert rpc.rejections == after_first, "later backfills must not re-probe"


@pytest.mark.asyncio
async def test_cap_survives_a_backfill_that_raises():
    """The shrink already paid for has to outlive the raise — which is why the cap is
    a shared object and not a return value."""
    rpc = RangeCappedRPC(cap=4)
    cap = chain.RangeCap(2000)

    with pytest.raises(RuntimeError):
        await run_backfill(rpc, cap, 1000, 1100)

    assert cap.value == 10, "shrank to the floor before giving up"
    rejections = rpc.rejections
    with pytest.raises(RuntimeError):
        await run_backfill(rpc, cap, 1000, 1100)
    assert rpc.rejections == rejections + 1, "one attempt at the floor, no re-probe"


@pytest.mark.asyncio
async def test_rate_limits_do_not_shrink_the_cap():
    """The trap that persistence introduces: shrinking on a transient 429 would pin the
    watcher at 10 blocks for the life of the process. Resetting every call used to make
    that mistake self-healing; now the error has to be classified."""
    request = httpx.Request("POST", "http://rpc")
    too_many = httpx.HTTPStatusError(
        "429", request=request, response=httpx.Response(429, request=request)
    )
    rpc = RangeCappedRPC(cap=10, error=too_many)
    cap = chain.RangeCap(2000)

    with pytest.raises(httpx.HTTPStatusError):
        await run_backfill(rpc, cap, 1000, 1500)

    assert cap.value == 2000, "a rate limit is not a range rejection"


@pytest.mark.asyncio
async def test_chunks_cover_the_range_without_gaps_or_overlap():
    rpc = RangeCappedRPC(cap=10)
    await run_backfill(rpc, chain.RangeCap(10), 1000, 1034)

    assert rpc.ranges == [(1000, 1009), (1010, 1019), (1020, 1029), (1030, 1034)]
