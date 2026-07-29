"""`pmex-shadow replay` — reruns stored fills through policy with no side effects
(§10 Determinism, §3.7 design doc). This is the payoff for event-sourcing: "backtest
your guards" only works because `target_fills` is append-only ground truth and
`decide()` is pure.

Reuses the book snapshots the Phase 1 paper logger already captured (`events`,
component='paper') rather than re-fetching live books for historical fills — that's
what the book actually looked like near that fill, not an approximation.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from decimal import Decimal

import asyncpg

from pmex_shadow.config import BotConfig, PolicyFile
from pmex_shadow.market.cache import get_market_meta
from pmex_shadow.models import (
    BookSnapshot,
    Decision,
    Intent,
    LedgerState,
    Position,
    Side,
    TargetFill,
    TargetPolicyStats,
)
from pmex_shadow.policy.engine import decide

_BOOK_HISTORY_WINDOW = dt.timedelta(minutes=5)


async def resolve_target_addresses(conn: asyncpg.Connection, targets: list[str]) -> list[str]:
    """Bot configs reference targets by alias or raw address (design doc §2.1:
    `targets: [whale1, whale2]`) — resolve aliases against target_stats."""
    resolved = []
    for t in targets:
        if t.lower().startswith("0x"):
            resolved.append(t.lower())
            continue
        row = await conn.fetchrow("SELECT target FROM target_stats WHERE alias = $1", t)
        if row:
            resolved.append(row["target"])
    return resolved


async def _load_book_snapshot(conn: asyncpg.Connection, fill_id: int) -> BookSnapshot | None:
    row = await conn.fetchrow(
        "SELECT context FROM events WHERE component = 'paper' AND (context->>'fill_id')::int = $1 "
        "AND context ? 'book_snapshot' ORDER BY id LIMIT 1",
        fill_id,
    )
    if row is None:
        return None
    context = json.loads(row["context"]) if isinstance(row["context"], str) else row["context"]
    snap = context.get("book_snapshot")
    if not snap:
        return None
    return BookSnapshot(
        token_id=context.get("token_id", ""),
        bids=[(Decimal(p), Decimal(s)) for p, s in snap.get("bids", [])],
        asks=[(Decimal(p), Decimal(s)) for p, s in snap.get("asks", [])],
        taken_at=dt.datetime.fromisoformat(snap["taken_at"]),
    )


def _target_stats_row_to_policy_stats(row, status: str, position_before: Decimal) -> TargetPolicyStats:
    return TargetPolicyStats(
        size_p50=row["size_p50"] or Decimal(0),
        size_p60=row["size_p60"] or Decimal(0),
        size_p80=row["size_p80"] or Decimal(0),
        size_p95=row["size_p95"] or Decimal(0),
        status=status,
        position_before=position_before,
    )


async def run_replay(
    database_url: str,
    gamma_api_base_url: str,
    bot: BotConfig,
    policy_file: PolicyFile,
    from_ts: dt.datetime,
    to_ts: dt.datetime,
) -> list[Decision]:
    """Rerun stored fills for `bot`'s targets through `decide()`. Read-only: only
    SELECTs against target_fills/target_stats/events, never writes.
    """
    conn = await asyncpg.connect(database_url)
    market_cache: dict[str, object] = {}
    try:
        addresses = await resolve_target_addresses(conn, bot.targets)
        if not addresses:
            return []

        rows = await conn.fetch(
            """
            SELECT id, dedupe_key, target, token_id, side, price, size, notional_usd,
                   block_number, block_ts, detected_at, source
            FROM target_fills
            WHERE target = ANY($1) AND block_ts >= $2 AND block_ts <= $3
            ORDER BY block_ts ASC
            """,
            addresses, from_ts, to_ts,
        )

        policy = policy_file.profiles[bot.policy.profile]
        global_risk = policy_file.risk

        # Simulated per-bot ledger state, evolving through the replay window.
        sim_positions: dict[str, Position] = {}
        deployed_usd = Decimal(0)
        global_exposure_usd = Decimal(0)  # single-bot replay: this bot IS the whole exposure
        book_history: dict[str, list[BookSnapshot]] = defaultdict(list)
        target_position_running: dict[tuple[str, str], Decimal] = defaultdict(Decimal)  # (target, token) -> cumulative shares

        decisions: list[Decision] = []

        for r in rows:
            fill = TargetFill(
                dedupe_key=r["dedupe_key"], target=r["target"], token_id=r["token_id"],
                side=Side.BUY if r["side"] == "BUY" else Side.SELL, price=r["price"], size=r["size"],
                notional_usd=r["notional_usd"], block_number=r["block_number"], block_ts=r["block_ts"],
                detected_at=r["detected_at"], source=r["source"],
            )

            book = await _load_book_snapshot(conn, r["id"])
            if book is None:
                continue  # no captured book near this fill; can't evaluate slippage/vwap — skip from the replay set entirely (not a policy Skip, a data-availability gap)

            key = (fill.target, fill.token_id)
            position_before = target_position_running[key]
            target_position_running[key] += fill.size if fill.side == Side.BUY else -fill.size

            target_row = await conn.fetchrow(
                "SELECT size_p50, size_p60, size_p80, size_p95, status FROM target_stats WHERE target = $1",
                fill.target,
            )
            if target_row is None:
                continue
            target_stats = _target_stats_row_to_policy_stats(target_row, target_row["status"], position_before)

            if fill.token_id not in market_cache:
                market_cache[fill.token_id] = await get_market_meta(gamma_api_base_url, fill.token_id)
            market = market_cache[fill.token_id]
            if market is None:
                continue

            history_window = [b for b in book_history[fill.token_id] if r["block_ts"] - b.taken_at <= _BOOK_HISTORY_WINDOW]

            ledger = LedgerState(
                positions=tuple(sim_positions.values()),
                deployed_usd=deployed_usd,
                global_exposure_usd=global_exposure_usd,
                halted=False,
            )

            decision = decide(
                fill=fill, book=book, book_history=tuple(history_window), bot=bot, policy=policy,
                global_risk=global_risk, ledger=ledger, target=target_stats, market=market,
                now=r["block_ts"],
            )
            decisions.append(decision)
            book_history[fill.token_id].append(book)

            if isinstance(decision, Intent):
                pos = sim_positions.get(decision.token_id)
                if decision.side == Side.BUY:
                    if pos is None:
                        sim_positions[decision.token_id] = Position(
                            token_id=decision.token_id, shares=decision.shares, cost_basis_usd=decision.notional_usd
                        )
                    else:
                        sim_positions[decision.token_id] = Position(
                            token_id=decision.token_id, shares=pos.shares + decision.shares,
                            cost_basis_usd=pos.cost_basis_usd + decision.notional_usd,
                        )
                    deployed_usd += decision.notional_usd
                    global_exposure_usd += decision.notional_usd
                else:
                    if pos is not None:
                        remaining = pos.shares - decision.shares
                        cost_reduction = (decision.shares / pos.shares) * pos.cost_basis_usd if pos.shares > 0 else Decimal(0)
                        deployed_usd = max(deployed_usd - cost_reduction, Decimal(0))
                        global_exposure_usd = max(global_exposure_usd - cost_reduction, Decimal(0))
                        if remaining > 0:
                            sim_positions[decision.token_id] = Position(
                                token_id=decision.token_id, shares=remaining, cost_basis_usd=pos.cost_basis_usd - cost_reduction
                            )
                        else:
                            sim_positions.pop(decision.token_id, None)

        # Netting (FR-P-10) isn't applied here: it collapses intents that are pending
        # *simultaneously* in a bot's live execution queue, which replay — processing
        # fills strictly in chronological order, one at a time — never has. Applying
        # net_intents() across an entire chronological run would incorrectly cancel a
        # buy against an unrelated sell that happened to occur weeks apart on the same
        # token. `analyze.py` can flag genuinely-simultaneous opposing intents in this
        # decision list itself if that turns out to matter.
        return decisions
    finally:
        await conn.close()
