"""Core value types (PRD §6). Frozen dataclasses only — no I/O, no behavior.

These are the types `policy.engine.decide()` (Phase 2) closes over. Keeping them here,
dependency-free, is what makes the policy engine a pure function: `decide()` takes and
returns only these plus primitives, so it can be replayed byte-for-byte against stored
data (§10 Determinism).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class TargetFill:
    dedupe_key: str
    target: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    notional_usd: Decimal
    block_number: int | None
    block_ts: datetime
    detected_at: datetime
    source: Literal["chain", "dataapi"]


@dataclass(frozen=True)
class BookSnapshot:
    token_id: str
    bids: list[tuple[Decimal, Decimal]]  # (price, size), best first
    asks: list[tuple[Decimal, Decimal]]
    taken_at: datetime

    def vwap_for(self, side: Side, usd: Decimal) -> tuple[Decimal, Decimal]:
        """Walk the book for `usd` notional on `side`; return (vwap_price, shares_filled).

        A BUY walks the asks (you buy at what sellers are asking); a SELL walks the
        bids. `shares_filled` may be less than `usd / best_price` if the book is too
        thin to absorb the full notional — that's the point of simulating this rather
        than assuming top-of-book fills (design doc §3.4a: this is what tells you
        whether a target's edge survives your latency and size).
        """
        levels = self.asks if side == Side.BUY else self.bids
        remaining_usd = usd
        total_shares = Decimal(0)
        total_cost = Decimal(0)
        for price, size in levels:
            if remaining_usd <= 0:
                break
            level_usd = price * size
            if level_usd <= remaining_usd:
                total_shares += size
                total_cost += level_usd
                remaining_usd -= level_usd
            else:
                shares_here = remaining_usd / price
                total_shares += shares_here
                total_cost += remaining_usd
                remaining_usd = Decimal(0)
        if total_shares == 0:
            return (Decimal(0), Decimal(0))
        vwap_price = total_cost / total_shares
        return (vwap_price, total_shares)


@dataclass(frozen=True)
class Intent:
    bot_id: str
    fill: TargetFill
    token_id: str
    side: Side
    limit_price: Decimal
    shares: Decimal
    notional_usd: Decimal
    target_percentile: Decimal
    size_multiplier: Decimal


@dataclass(frozen=True)
class Skip:
    bot_id: str
    fill: TargetFill
    reason: str  # stable machine-readable token, see PRD §6


Decision = Intent | Skip

# Stable skip reasons (PRD §6). Extend, never rename — the dashboard groups on these.
SKIP_REASONS = frozenset(
    {
        "selector_category",
        "selector_liquidity",
        "selector_notional",
        "selector_resolution_window",
        "below_target_percentile",
        "slippage_guard",
        "volatility_guard",
        "stale_fill",
        "envelope_exhausted",
        "max_concurrent_positions",
        "global_exposure_cap",
        "below_min_order",
        "unknown_category",
        "market_not_tradeable",
        "target_paused",
        "netted_out",
        "bot_halted",
    }
)
