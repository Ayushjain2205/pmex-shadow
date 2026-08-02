"""target_stats computation (FR-T-1): size percentiles, 30d fill count, hit rate,
PnL, reversal rate. Meant to run on a scheduled job (design doc §3.6), not the hot
path — `policy.sizing.percentile_of_value` (Phase 2) is what actually consumes the
percentile columns this writes.

hit_rate_30d / pnl_30d_usd are the target's *own* trading performance (not ours),
from the SDK's list_closed_positions() — realized_pnl on resolved trades, not
list_positions()'s cash_pnl (an unrealized floating mark on still-*open* positions,
which produced 0% hit rates and six-figure "losses" for every target here: an open
position's floating mark isn't a win/loss signal, and list_positions() has no date
filter at all, so every recompute pass was summing a high-frequency trader's entire
position history as if it were 30 days). Sorted DESC by timestamp and paginated
with an early stop the moment a page's positions age past the window — also fixes
the unbounded pagination the old code did on every 15-minute recompute pass.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import asyncpg
from polymarket import PRODUCTION
from polymarket.clients.async_public import AsyncPublicClient

from pmex_shadow.targets.adversarial import compute_reversal_rate


def _percentile(sorted_values: list[Decimal], pct: float) -> Decimal:
    if not sorted_values:
        return Decimal(0)
    idx = min(int(len(sorted_values) * pct / 100), len(sorted_values) - 1)
    return sorted_values[idx]


async def compute_size_percentiles(conn: asyncpg.Connection, target: str, since_days: int = 30) -> dict[str, Decimal]:
    rows = await conn.fetch(
        "SELECT notional_usd FROM target_fills WHERE target = $1 AND block_ts >= now() - make_interval(days => $2)",
        target, since_days,
    )
    values = sorted(r["notional_usd"] for r in rows)
    return {
        "size_p50": _percentile(values, 50), "size_p60": _percentile(values, 60),
        "size_p80": _percentile(values, 80), "size_p95": _percentile(values, 95),
        "fills_30d": len(values),
    }


async def compute_hit_rate_and_pnl(target: str, since_days: int = 30) -> tuple[Decimal | None, Decimal | None]:
    client = AsyncPublicClient(environment=PRODUCTION)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
    wins = 0
    total = 0
    pnl = Decimal(0)
    async for page in client.list_closed_positions(user=target, sort_by="TIMESTAMP", sort_direction="DESC", page_size=50):
        reached_cutoff = False
        for p in page.items:
            if p.realized_pnl is None or p.timestamp is None:
                continue
            if p.timestamp < cutoff:
                reached_cutoff = True
                break
            total += 1
            pnl += p.realized_pnl
            if p.realized_pnl > 0:
                wins += 1
        if reached_cutoff:
            break
    if total == 0:
        return None, None
    return Decimal(wins) / Decimal(total), pnl


async def recompute_target_stats(conn: asyncpg.Connection, target: str, reversal_window_s: int = 60) -> None:
    percentiles = await compute_size_percentiles(conn, target)
    hit_rate, pnl = await compute_hit_rate_and_pnl(target)
    reversal_rate = await compute_reversal_rate(conn, target, reversal_window_s)

    last_fill = await conn.fetchrow("SELECT max(block_ts) AS last_fill_at FROM target_fills WHERE target = $1", target)

    await conn.execute(
        """
        UPDATE target_stats SET
            size_p50 = $2, size_p60 = $3, size_p80 = $4, size_p95 = $5,
            fills_30d = $6, hit_rate_30d = $7, pnl_30d_usd = $8, reversal_rate = $9,
            last_fill_at = $10, computed_at = now()
        WHERE target = $1
        """,
        target, percentiles["size_p50"], percentiles["size_p60"], percentiles["size_p80"], percentiles["size_p95"],
        percentiles["fills_30d"], hit_rate, pnl, reversal_rate, last_fill["last_fill_at"],
    )


async def recompute_all_targets(conn: asyncpg.Connection, reversal_window_s: int = 60) -> int:
    targets = await conn.fetch("SELECT target FROM target_stats")
    for row in targets:
        await recompute_target_stats(conn, row["target"], reversal_window_s)
    return len(targets)
