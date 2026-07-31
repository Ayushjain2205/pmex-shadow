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
class MarketMeta:
    """Cached token metadata (FR-M-1) needed by selectors. `decide()` (§6) must stay
    pure — it can't fetch this itself — so it's passed in as data, resolved by the
    caller from `market/cache.py` before calling `decide()`.
    """

    token_id: str
    category: str | None  # None = unknown/uncached (FR-M-3: scoped bots must skip, never guess)
    tick_size: Decimal
    min_order_size: Decimal
    neg_risk: bool
    tradeable: bool
    event_id: str | None
    resolution_days_out: int | None  # days from `now` to resolution; None if unknown


@dataclass(frozen=True)
class ResolutionStatus:
    """Gamma's `closed`/`outcomePrices` view of a market, used to credit *paper*
    positions on resolution (ledger/redeem.py) — deliberately not conflated with the
    on-chain `payoutDenominator` check FR-L-6 requires for live redemption
    (docs/VERIFIED.md item 9: "a market merely showing 'resolved' in the UI" is
    explicitly called out there as distinct from genuinely redeemable on-chain, and
    that distinction exists to avoid live mode wasting real gas on a transaction that
    isn't valid yet). Paper positions have no real on-chain holdings to check against
    in the first place, so Gamma's view is the only signal available for them, not a
    shortcut around the live-mode check.
    """

    token_id: str
    closed: bool
    won: bool | None  # None until closed; meaningless (not queried) if closed is False


@dataclass(frozen=True)
class TargetPolicyStats:
    """The slice of `target_stats` (§5) `decide()` needs: the size distribution for
    FR-P-3's percentile sizing, plus status (FR-T-2, FR-T-3) and the caller-computed
    position estimate FR-P-11's exit-proportionality needs.

    `position_before`: the target's own share count in `fill.token_id` immediately
    before this fill — not a `target_stats` column (that table holds aggregate stats,
    not per-token position). §5 doesn't model per-token target position anywhere, so
    this is a deliberate extension: the caller (ledger/replay) derives it by replaying
    that target's own fill history for this token before calling `decide()`. Without
    it, FR-P-11 ("sell X% of yours when they sell X% of theirs") isn't computable at
    all from the given schema.
    """

    size_p50: Decimal
    size_p60: Decimal
    size_p80: Decimal
    size_p95: Decimal
    status: Literal["active", "paused_decay", "paused_dormant", "paused_manual"]
    position_before: Decimal


@dataclass(frozen=True)
class Position:
    token_id: str
    shares: Decimal
    cost_basis_usd: Decimal


@dataclass(frozen=True)
class LedgerState:
    """Everything `decide()` needs to know about capital state (FR-P-5, FR-P-11).
    Computed by the caller from the `positions` table (+ a cross-bot read for the
    global cap, per design doc §2.2: "no individual bot can enforce
    global_max_exposure_usd — it must be a read against the shared positions table").
    """

    positions: tuple[Position, ...]  # this bot's own open positions
    deployed_usd: Decimal  # this bot's capital currently committed (sum of open cost basis)
    global_exposure_usd: Decimal  # across ALL bots, for the shared cap
    halted: bool  # FR-L-5: reconciler halts the bot on drift; decide() must respect it
    realized_pnl_usd: Decimal  # all-time realized PnL, every position regardless of lifecycle —
    # the envelope check needs this or it has no memory of cumulative losses: a
    # position leaving 'open' drops out of deployed_usd and its capital reads as
    # fully "returned" whether it actually redeemed at cost or was a total loss.
    # Without this, decide() keeps sizing new trades against the full original
    # envelope forever, even after realized losses have eaten into (or exceeded) it.

    def position_for(self, token_id: str) -> Position | None:
        for p in self.positions:
            if p.token_id == token_id:
                return p
        return None


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
        # Extended beyond the PRD §6 list (which explicitly permits extending, never
        # renaming): a target SELL with nothing on our side to mirror doesn't fit any
        # of the above — it's not a capital/guard/selector rejection, there's simply
        # no position to scale FR-P-11's exit fraction against.
        "no_position_to_exit",
    }
)
