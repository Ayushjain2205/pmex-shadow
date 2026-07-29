"""`pmex-shadow analyze` — per-target scorecards and pairwise correlation (design doc
§3.7). PnL here is genuinely hypothetical and partial: full realized/hit-rate stats
are FR-T-1 (Phase 5) once `target_stats` is populated by a scheduled job — this reads
whatever's already there rather than recomputing it, and is explicit in its output
about what it couldn't compute (e.g. hit rate before Phase 5 exists).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from itertools import combinations

import asyncpg


@dataclasses.dataclass(frozen=True)
class TargetScorecard:
    target: str
    alias: str | None
    fills: int
    avg_detection_latency_s: float | None
    avg_slippage_vs_target: Decimal | None
    hit_rate_30d: Decimal | None
    pnl_30d_usd: Decimal | None
    status: str


@dataclasses.dataclass(frozen=True)
class CorrelationPair:
    target_a: str
    target_b: str
    co_occurrences: int
    total_a: int
    total_b: int


async def target_scorecards(conn: asyncpg.Connection, since: dt.datetime) -> list[TargetScorecard]:
    rows = await conn.fetch(
        """
        SELECT t.target, t.alias, t.hit_rate_30d, t.pnl_30d_usd, t.status,
               count(f.id) AS fills,
               avg(extract(epoch FROM (f.detected_at - f.block_ts)))
                   FILTER (WHERE f.source = 'chain') AS avg_latency_s
        FROM target_stats t
        LEFT JOIN target_fills f ON f.target = t.target AND f.block_ts >= $1
        GROUP BY t.target, t.alias, t.hit_rate_30d, t.pnl_30d_usd, t.status
        ORDER BY fills DESC
        """,
        since,
    )

    scorecards = []
    for r in rows:
        slippage_rows = await conn.fetch(
            """
            SELECT (context->>'sim_slippage_vs_target')::numeric AS slippage
            FROM events
            WHERE component = 'paper' AND context->>'target' = $1
              AND context->>'sim_slippage_vs_target' IS NOT NULL
              AND at >= $2
            """,
            r["target"], since,
        )
        slippages = [s["slippage"] for s in slippage_rows if s["slippage"] is not None]
        avg_slippage = sum(slippages) / len(slippages) if slippages else None

        scorecards.append(TargetScorecard(
            target=r["target"], alias=r["alias"], fills=r["fills"],
            avg_detection_latency_s=float(r["avg_latency_s"]) if r["avg_latency_s"] is not None else None,
            avg_slippage_vs_target=avg_slippage, hit_rate_30d=r["hit_rate_30d"],
            pnl_30d_usd=r["pnl_30d_usd"], status=r["status"],
        ))
    return scorecards


async def pairwise_correlation(conn: asyncpg.Connection, since: dt.datetime, window_s: int = 5) -> list[CorrelationPair]:
    """Flags target pairs that repeatedly fire on the same side of the same token
    within `window_s` of each other — "one signal at 2x size" (design doc §3.7), the
    thing you want to know about before a drawdown teaches you.
    """
    rows = await conn.fetch(
        "SELECT target, token_id, side, block_ts FROM target_fills WHERE block_ts >= $1 ORDER BY block_ts",
        since,
    )
    by_target: dict[str, int] = {}
    events = []
    for r in rows:
        by_target[r["target"]] = by_target.get(r["target"], 0) + 1
        events.append(r)

    co_occurrences: dict[tuple[str, str], int] = {}
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            if (b["block_ts"] - a["block_ts"]).total_seconds() > window_s:
                break
            if a["target"] == b["target"] or a["token_id"] != b["token_id"] or a["side"] != b["side"]:
                continue
            key = tuple(sorted((a["target"], b["target"])))
            co_occurrences[key] = co_occurrences.get(key, 0) + 1

    return [
        CorrelationPair(target_a=a, target_b=b, co_occurrences=count, total_a=by_target.get(a, 0), total_b=by_target.get(b, 0))
        for (a, b), count in sorted(co_occurrences.items(), key=lambda kv: -kv[1])
        if count > 0
    ]
