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

        Implemented in Phase 1 (paper fill simulation, FR-EXE-9). Left unimplemented in
        Phase 0 — the type exists now so downstream modules can be built against it.
        """
        raise NotImplementedError("BookSnapshot.vwap_for is implemented in Phase 1")


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
