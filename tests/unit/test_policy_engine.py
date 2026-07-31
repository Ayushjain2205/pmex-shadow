import datetime as dt
from decimal import Decimal

import pytest

from pmex_shadow.config import (
    BotConfig,
    PolicyProfile,
    RiskConfig,
    RiskGlobalConfig,
    SelectorsConfig,
    SizingConfig,
    SizingCurvePoint,
    VolatilityGuardConfig,
    WalletConfig,
    PolicyRef,
)
from pmex_shadow.models import (
    BookSnapshot,
    Intent,
    LedgerState,
    MarketMeta,
    Position,
    Side,
    Skip,
    TargetFill,
    TargetPolicyStats,
)
from pmex_shadow.policy.engine import decide

NOW = dt.datetime(2026, 7, 29, 12, 0, 0, tzinfo=dt.timezone.utc)


def make_bot(**overrides) -> BotConfig:
    defaults = dict(
        name="test_bot",
        mode="paper",
        wallet=WalletConfig(funder_env="X_FUNDER", pk_env="X_PK"),
        selectors=SelectorsConfig(),
        targets=["whale1"],
        policy=PolicyRef(profile="tight"),
        risk=RiskConfig(envelope_usd=Decimal("500")),
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def make_policy(**overrides) -> PolicyProfile:
    sizing_overrides = overrides.pop("sizing_overrides", {})
    sizing_defaults = dict(
        base_unit_usd=Decimal("25"),
        curve=[
            SizingCurvePoint(p=50, mult=Decimal("1.0")),
            SizingCurvePoint(p=80, mult=Decimal("1.5")),
            SizingCurvePoint(p=95, mult=Decimal("2.5")),
        ],
        min_target_size_percentile=Decimal("60"),
        min_order_usd=Decimal("5"),
        max_position_usd=Decimal("75"),
        max_concurrent_positions=8,
        reserve_pct=Decimal("20"),
    )
    sizing_defaults.update(sizing_overrides)

    defaults = dict(
        max_slippage_ticks=2,
        volatility_guard=VolatilityGuardConfig(window_s=5, max_ticks=2),
        max_fill_age_s=30,
        sizing=SizingConfig(**sizing_defaults),
    )
    defaults.update(overrides)
    return PolicyProfile(**defaults)


def make_target(**overrides) -> TargetPolicyStats:
    defaults = dict(
        size_p50=Decimal("2000"),
        size_p60=Decimal("3000"),
        size_p80=Decimal("6000"),
        size_p95=Decimal("15000"),
        status="active",
        position_before=Decimal("0"),
    )
    defaults.update(overrides)
    return TargetPolicyStats(**defaults)


def make_fill(**overrides) -> TargetFill:
    defaults = dict(
        dedupe_key="tx1:0",
        target="0xtarget",
        token_id="tok1",
        side=Side.BUY,
        price=Decimal("0.62"),
        size=Decimal("16129.03"),
        notional_usd=Decimal("10000"),
        block_number=100,
        block_ts=NOW,
        detected_at=NOW,
        source="chain",
    )
    defaults.update(overrides)
    return TargetFill(**defaults)


def make_book(**overrides) -> BookSnapshot:
    defaults = dict(
        token_id="tok1",
        bids=[(Decimal("0.61"), Decimal("100000"))],
        asks=[(Decimal("0.63"), Decimal("100000"))],
        taken_at=NOW,
    )
    defaults.update(overrides)
    return BookSnapshot(**defaults)


def make_market(**overrides) -> MarketMeta:
    defaults = dict(
        token_id="tok1",
        category=None,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        neg_risk=False,
        tradeable=True,
        event_id=None,
        resolution_days_out=None,
    )
    defaults.update(overrides)
    return MarketMeta(**defaults)


def make_ledger(**overrides) -> LedgerState:
    other_positions = tuple(
        Position(token_id=f"other{i}", shares=Decimal("10"), cost_basis_usd=Decimal("50"))
        for i in range(6)
    )
    defaults = dict(
        positions=other_positions,
        deployed_usd=Decimal("310"),
        global_exposure_usd=Decimal("1000"),
        halted=False,
        realized_pnl_usd=Decimal("0"),
    )
    defaults.update(overrides)
    return LedgerState(**defaults)


def make_global_risk(**overrides) -> RiskGlobalConfig:
    defaults = dict(
        global_max_exposure_usd=Decimal("5000"),
        max_orders_per_minute=30,
        halt_on_reconcile_drift_usd=Decimal("100"),
    )
    defaults.update(overrides)
    return RiskGlobalConfig(**defaults)


def run_decide(**kwargs):
    args = dict(
        fill=make_fill(),
        book=make_book(),
        book_history=(),
        bot=make_bot(),
        policy=make_policy(),
        global_risk=make_global_risk(),
        ledger=make_ledger(),
        target=make_target(),
        market=make_market(),
        now=NOW,
    )
    args.update(kwargs)
    return decide(**args)


# --- The worked example (design doc §3.3 / PRD §8 Phase 2 acceptance) ---


def test_worked_example_reproduces_exactly():
    """$10k fill at target's p88 (their p50=$2k,p80=$6k,p95=$15k) -> 2.0x -> $50 -> 79
    shares, limit 0.64. Percentile/multiplier land at ~87.51/~2.0007 under sqrt-space
    interpolation (see sizing.py docstring) -- close enough to round to the design
    doc's "p88"/"2.0x", and the resulting notional ($49.77 = 79 * $0.63) matches the
    worked example's own parenthetical exactly.
    """
    decision = run_decide()

    assert isinstance(decision, Intent)
    assert decision.side == Side.BUY
    assert decision.shares == Decimal("79")
    assert decision.notional_usd == Decimal("49.77")
    assert decision.limit_price == Decimal("0.64")
    assert round(decision.target_percentile) == 88
    assert round(decision.size_multiplier, 1) == Decimal("2.0")


# --- Purity (FR-P-1) ---


def test_decide_is_pure_identical_inputs_identical_outputs():
    kwargs = dict(
        fill=make_fill(), book=make_book(), book_history=(), bot=make_bot(),
        policy=make_policy(), global_risk=make_global_risk(), ledger=make_ledger(),
        target=make_target(), market=make_market(), now=NOW,
    )
    result1 = decide(**kwargs)
    result2 = decide(**kwargs)
    assert result1 == result2


def test_decide_has_no_import_time_or_call_time_io():
    import inspect

    from pmex_shadow.policy import engine

    source = inspect.getsource(engine)
    for banned in ("open(", "requests.", "httpx.", "asyncpg.", "socket.", "urlopen"):
        assert banned not in source, f"found forbidden I/O call: {banned}"


# --- Skip reasons (FR-P-2, FR-P-7..9, FR-L-5, FR-T-2..4) ---


def test_skip_bot_halted():
    d = run_decide(ledger=make_ledger(halted=True))
    assert isinstance(d, Skip) and d.reason == "bot_halted"


@pytest.mark.parametrize("status", ["paused_decay", "paused_dormant", "paused_manual"])
def test_skip_target_paused(status):
    d = run_decide(target=make_target(status=status))
    assert isinstance(d, Skip) and d.reason == "target_paused"


def test_skip_stale_fill():
    old_fill = make_fill(block_ts=NOW - dt.timedelta(seconds=60))
    d = run_decide(fill=old_fill)
    assert isinstance(d, Skip) and d.reason == "stale_fill"
    assert d.detail == {"age_s": 60.0, "max_fill_age_s": 30}


def test_skip_unknown_category():
    bot = make_bot(selectors=SelectorsConfig(categories=["sports"]))
    d = run_decide(bot=bot, market=make_market(category=None))
    assert isinstance(d, Skip) and d.reason == "unknown_category"


def test_skip_selector_category():
    bot = make_bot(selectors=SelectorsConfig(categories=["sports"]))
    d = run_decide(bot=bot, market=make_market(category="politics"))
    assert isinstance(d, Skip) and d.reason == "selector_category"


def test_selector_category_passes_when_matching():
    bot = make_bot(selectors=SelectorsConfig(categories=["sports"]))
    d = run_decide(bot=bot, market=make_market(category="sports"))
    assert isinstance(d, Intent)


def test_skip_selector_liquidity():
    bot = make_bot(selectors=SelectorsConfig(min_book_liquidity_usd=Decimal("100000")))
    thin_book = make_book(bids=[(Decimal("0.61"), Decimal("1"))], asks=[(Decimal("0.63"), Decimal("1"))])
    d = run_decide(bot=bot, book=thin_book)
    assert isinstance(d, Skip) and d.reason == "selector_liquidity"


def test_skip_selector_notional():
    bot = make_bot(selectors=SelectorsConfig(min_target_notional_usd=Decimal("50000")))
    d = run_decide(bot=bot)
    assert isinstance(d, Skip) and d.reason == "selector_notional"


def test_skip_selector_resolution_window():
    bot = make_bot(selectors=SelectorsConfig(max_time_to_resolution_days=7))
    d = run_decide(bot=bot, market=make_market(resolution_days_out=30))
    assert isinstance(d, Skip) and d.reason == "selector_resolution_window"


def test_skip_market_not_tradeable():
    d = run_decide(market=make_market(tradeable=False))
    assert isinstance(d, Skip) and d.reason == "market_not_tradeable"


def test_skip_below_target_percentile():
    # A small fill (well under p50) sits at a low percentile.
    small_fill = make_fill(notional_usd=Decimal("100"))
    d = run_decide(fill=small_fill)
    assert isinstance(d, Skip) and d.reason == "below_target_percentile"


def test_skip_slippage_guard():
    # Best ask far beyond tolerance (2 ticks = 0.02) from the target's fill price.
    wide_book = make_book(asks=[(Decimal("0.90"), Decimal("100000"))])
    d = run_decide(book=wide_book)
    assert isinstance(d, Skip) and d.reason == "slippage_guard"
    assert d.detail == {"target_price": "0.62", "best_price": "0.90", "adverse_ticks": 28.0, "max_slippage_ticks": 2}


def test_skip_volatility_guard():
    history = (make_book(taken_at=NOW - dt.timedelta(seconds=1), bids=[(Decimal("0.30"), Decimal("1"))], asks=[(Decimal("0.32"), Decimal("1"))]),)
    d = run_decide(book_history=history)
    assert isinstance(d, Skip) and d.reason == "volatility_guard"


def test_skip_envelope_exhausted():
    d = run_decide(ledger=make_ledger(deployed_usd=Decimal("400")))  # deployable is 400 (80% of 500)
    assert isinstance(d, Skip) and d.reason == "envelope_exhausted"


def test_cumulative_realized_losses_shrink_the_envelope():
    """A bot that already lost its whole envelope must not keep sizing new trades
    against the original, untouched envelope_usd — deployed_usd alone (currently-open
    exposure) can't see this, since a closed position drops out of it regardless of
    whether it closed at a profit or a total loss."""
    d = run_decide(ledger=make_ledger(deployed_usd=Decimal("0"), realized_pnl_usd=Decimal("-500")))
    assert isinstance(d, Skip) and d.reason == "envelope_exhausted"


def test_realized_gains_grow_the_envelope():
    d = run_decide(ledger=make_ledger(deployed_usd=Decimal("0"), realized_pnl_usd=Decimal("500")))
    assert isinstance(d, Intent)


def test_skip_global_exposure_cap():
    d = run_decide(global_risk=make_global_risk(global_max_exposure_usd=Decimal("1000")), ledger=make_ledger(global_exposure_usd=Decimal("1000")))
    assert isinstance(d, Skip) and d.reason == "global_exposure_cap"


def test_skip_max_concurrent_positions():
    eight_positions = tuple(
        Position(token_id=f"other{i}", shares=Decimal("10"), cost_basis_usd=Decimal("10"))
        for i in range(8)
    )
    d = run_decide(ledger=make_ledger(positions=eight_positions, deployed_usd=Decimal("80")))
    assert isinstance(d, Skip) and d.reason == "max_concurrent_positions"


def test_max_concurrent_positions_does_not_block_adding_to_existing_token():
    eight_positions = tuple(
        Position(token_id=f"other{i}", shares=Decimal("10"), cost_basis_usd=Decimal("10"))
        for i in range(7)
    ) + (Position(token_id="tok1", shares=Decimal("5"), cost_basis_usd=Decimal("3")),)
    d = run_decide(ledger=make_ledger(positions=eight_positions, deployed_usd=Decimal("80")))
    assert isinstance(d, Intent)


def test_skip_below_min_order():
    policy = make_policy(sizing_overrides={"min_order_usd": Decimal("1000")})
    d = run_decide(policy=policy)
    assert isinstance(d, Skip) and d.reason == "below_min_order"


def test_skip_no_position_to_exit():
    sell_fill = make_fill(side=Side.SELL, notional_usd=Decimal("1000"), size=Decimal("1000"))
    d = run_decide(fill=sell_fill, target=make_target(position_before=Decimal("5000")))
    assert isinstance(d, Skip) and d.reason == "no_position_to_exit"


# --- Exit proportionality (FR-P-11) ---


def test_exit_sizing_is_proportional_never_absolute():
    """Target sells 40% of a 1000-share holding; we hold 79 shares -> sell 40% of 79,
    not 400 shares (which would flatten and then short us at a 200:1 size ratio)."""
    our_position = Position(token_id="tok1", shares=Decimal("79"), cost_basis_usd=Decimal("49.77"))
    ledger = make_ledger(positions=make_ledger().positions + (our_position,))
    sell_fill = make_fill(side=Side.SELL, price=Decimal("0.65"), notional_usd=Decimal("260"), size=Decimal("400"))
    target = make_target(position_before=Decimal("1000"))

    decision = run_decide(fill=sell_fill, ledger=ledger, target=target, book=make_book(bids=[(Decimal("0.64"), Decimal("100000"))]))

    assert isinstance(decision, Intent)
    assert decision.side == Side.SELL
    assert decision.shares == Decimal("31")  # floor(79 * 0.4) = floor(31.6)
