"""decide(): the pure policy function (PRD §6, FR-P-1..12).

No I/O, no clock reads (`now` is always an argument), no network — this is what makes
it replayable byte-for-byte against stored data (§10 Determinism) and unit-testable
with frozen inputs (FR-P-1). The signature here is wider than §6's literal
`decide(fill, book, bot, ledger, target, now)`: `book_history` (FR-P-8's volatility
guard needs a window, not one snapshot) and `market` (the selectors need category/
tick-size/tradeable data `decide()` can't fetch itself) are deliberate, documented
extensions — seeThe module docstrings on `MarketMeta`/`TargetPolicyStats` in
models.py for why. `policy` (the resolved PolicyProfile) and `global_risk` are passed
separately from `bot` because `BotConfig` only references a profile *name*
(`bot.policy.profile`) and holds only its own envelope, not the shared-across-bots
global cap.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pmex_shadow.config import BotConfig, PolicyProfile, RiskGlobalConfig
from pmex_shadow.models import (
    BookSnapshot,
    Decision,
    Intent,
    LedgerState,
    MarketMeta,
    Side,
    Skip,
    TargetFill,
    TargetPolicyStats,
)
from pmex_shadow.policy.guards import slippage_guard, staleness_guard, volatility_guard
from pmex_shadow.policy.sizing import exit_shares, multiplier_for_percentile, percentile_of_value, shares_from_usd

_PAUSED_STATUSES = {"paused_decay", "paused_dormant", "paused_manual"}


def decide(
    *,
    fill: TargetFill,
    book: BookSnapshot,
    book_history: tuple[BookSnapshot, ...],
    bot: BotConfig,
    policy: PolicyProfile,
    global_risk: RiskGlobalConfig,
    ledger: LedgerState,
    target: TargetPolicyStats,
    market: MarketMeta,
    now: dt.datetime,
) -> Decision:
    def skip(reason: str) -> Skip:
        return Skip(bot_id=bot.name, fill=fill, reason=reason)

    # --- Bot/target state (FR-L-5, FR-T-2..4) ---
    if ledger.halted:
        return skip("bot_halted")
    if target.status in _PAUSED_STATUSES:
        return skip("target_paused")

    # --- Staleness (FR-P-9) ---
    if not staleness_guard(fill, policy.max_fill_age_s, now):
        return skip("stale_fill")

    # --- Selectors (FR-P-2): AND, absent = no constraint ---
    sel = bot.selectors
    if sel.categories is not None:
        if market.category is None:
            return skip("unknown_category")  # FR-M-3: fail closed, never guess
        if market.category not in sel.categories:
            return skip("selector_category")
    if sel.min_book_liquidity_usd is not None:
        liquidity = sum((p * s for p, s in book.bids), Decimal(0)) + sum((p * s for p, s in book.asks), Decimal(0))
        if liquidity < sel.min_book_liquidity_usd:
            return skip("selector_liquidity")
    if sel.min_target_notional_usd is not None and fill.notional_usd < sel.min_target_notional_usd:
        return skip("selector_notional")
    if sel.max_time_to_resolution_days is not None:
        if market.resolution_days_out is None or market.resolution_days_out > sel.max_time_to_resolution_days:
            return skip("selector_resolution_window")
    if not market.tradeable:
        return skip("market_not_tradeable")

    # --- Exit path (FR-P-11): proportional to OUR position, never their absolute size ---
    if fill.side == Side.SELL:
        our_position = ledger.position_for(fill.token_id)
        shares = exit_shares(fill, target, our_position)
        if shares <= 0:
            return skip("no_position_to_exit")

        ref_price = slippage_guard(fill, book, policy.max_slippage_ticks, market.tick_size)
        if ref_price is None:
            return skip("slippage_guard")
        if not volatility_guard(
            book, book_history, policy.volatility_guard.window_s, policy.volatility_guard.max_ticks,
            market.tick_size, now,
        ):
            return skip("volatility_guard")

        return Intent(
            bot_id=bot.name,
            fill=fill,
            token_id=fill.token_id,
            side=Side.SELL,
            limit_price=ref_price,
            shares=shares,
            notional_usd=shares * ref_price,
            target_percentile=Decimal(0),  # sizing curve doesn't apply to exits
            size_multiplier=Decimal(1),
        )

    # --- Entry path (FR-P-3, FR-P-4, FR-P-6) ---
    percentile = percentile_of_value(fill.notional_usd, target)
    if percentile < policy.sizing.min_target_size_percentile:
        return skip("below_target_percentile")

    ref_price = slippage_guard(fill, book, policy.max_slippage_ticks, market.tick_size)
    if ref_price is None:
        return skip("slippage_guard")
    if not volatility_guard(
        book, book_history, policy.volatility_guard.window_s, policy.volatility_guard.max_ticks,
        market.tick_size, now,
    ):
        return skip("volatility_guard")

    mult = multiplier_for_percentile(percentile, policy.sizing.curve)
    sized_usd = policy.sizing.base_unit_usd * mult

    # Clamp cascade (FR-P-5): max_position_usd -> envelope -> global cap -> concurrent positions
    usd = min(sized_usd, policy.sizing.max_position_usd)

    deployable = bot.risk.envelope_usd * (Decimal(1) - policy.sizing.reserve_pct / Decimal(100))
    available = deployable - ledger.deployed_usd
    if available <= 0:
        return skip("envelope_exhausted")
    usd = min(usd, available)

    global_available = global_risk.global_max_exposure_usd - ledger.global_exposure_usd
    if global_available <= 0:
        return skip("global_exposure_cap")
    usd = min(usd, global_available)

    existing_position = ledger.position_for(fill.token_id)
    if existing_position is None and len(ledger.positions) >= policy.sizing.max_concurrent_positions:
        return skip("max_concurrent_positions")

    sized = shares_from_usd(usd, ref_price, policy.sizing.min_order_usd)
    if sized is None:
        return skip("below_min_order")
    shares, notional = sized

    # Limit price adds one tick beyond the reference price for marketability — sizing
    # against the bare reference price risks resting unfilled if the book ticks away
    # between snapshot and submission (worked example step 8: sized at best-ask 0.63,
    # submitted at 0.64).
    limit_price = ref_price + market.tick_size

    return Intent(
        bot_id=bot.name,
        fill=fill,
        token_id=fill.token_id,
        side=Side.BUY,
        limit_price=limit_price,
        shares=shares,
        notional_usd=notional,
        target_percentile=percentile,
        size_multiplier=mult,
    )
