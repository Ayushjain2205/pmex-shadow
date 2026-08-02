"""Dashboard rendering of the rule skips, driven by real decide() output.

The detail dicts are built in policy/engine.py and read in control/queries.py with
nothing typed between them, so the two drift silently — the rendering keeps working
against whatever shape it was written for while the engine emits something else, and
the only symptom is a skip row that reads worse than it should. Feeding genuine Skip
objects through the renderer is what pins them together.

Skip.detail also round-trips through JSONB on the way to the dashboard, so anything
asserted here has to survive json.dumps -- see the serialization test at the bottom.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from pmex_shadow.config import (
    BotConfig, MatchRule, PolicyProfile, PolicyRef, RiskConfig, RiskGlobalConfig,
    SelectorsConfig, SizingConfig, SizingCurvePoint, VolatilityGuardConfig, WalletConfig,
)
from pmex_shadow.control.queries import _skip_category, _skip_label, _skip_summary
from pmex_shadow.models import (
    SKIP_REASONS, BookSnapshot, LedgerState, MarketMeta, Side, Skip, TargetFill, TargetPolicyStats,
)
from pmex_shadow.policy.engine import decide

NOW = dt.datetime(2026, 8, 3, 12, 0, 0, tzinfo=dt.timezone.utc)

SOL_ATTRS = {
    "asset": frozenset({"sol"}),
    "duration": frozenset({"5m"}),
    "series": frozenset({"sol-up-or-down-5m"}),
    "tag": frozenset({"crypto-prices", "up-or-down"}),
}


def skip_for(selectors: SelectorsConfig, attrs=SOL_ATTRS) -> Skip:
    """Run the real engine and return the Skip it produced."""
    decision = decide(
        fill=TargetFill(
            dedupe_key="tx1:0", target="0xtarget", token_id="tok1", side=Side.BUY,
            price=Decimal("0.62"), size=Decimal("16129.03"), notional_usd=Decimal("10000"),
            block_number=100, block_ts=NOW, detected_at=NOW, source="chain",
        ),
        book=BookSnapshot(
            token_id="tok1", bids=[(Decimal("0.61"), Decimal("100000"))],
            asks=[(Decimal("0.63"), Decimal("100000"))], taken_at=NOW,
        ),
        book_history=(),
        bot=BotConfig(
            name="test_bot", mode="paper", wallet=WalletConfig(funder_env="F", pk_env="P"),
            selectors=selectors, targets=["whale1"], policy=PolicyRef(profile="tight"),
            risk=RiskConfig(envelope_usd=Decimal("500")),
        ),
        policy=_policy(),
        global_risk=RiskGlobalConfig(
            global_max_exposure_usd=Decimal("5000"), max_orders_per_minute=30,
            halt_on_reconcile_drift_usd=Decimal("100"),
        ),
        ledger=LedgerState(
            positions=(), deployed_usd=Decimal("0"), global_exposure_usd=Decimal("0"),
            halted=False, realized_pnl_usd=Decimal("0"),
        ),
        target=TargetPolicyStats(
            size_p50=Decimal("2000"), size_p60=Decimal("3000"), size_p80=Decimal("6000"),
            size_p95=Decimal("15000"), status="active", position_before=Decimal("0"),
        ),
        market=MarketMeta(
            token_id="tok1", category=None, tick_size=Decimal("0.01"), min_order_size=Decimal("5"),
            neg_risk=False, tradeable=True, event_id=None, resolution_days_out=None, attrs=attrs,
        ),
        now=NOW,
    )
    assert isinstance(decision, Skip), f"expected a Skip, got {decision!r}"
    return decision


def _policy() -> PolicyProfile:
    return PolicyProfile(
        max_slippage_ticks=2,
        volatility_guard=VolatilityGuardConfig(window_s=5, max_ticks=2),
        max_fill_age_s=30,
        sizing=SizingConfig(
            base_unit_usd=Decimal("25"),
            curve=[SizingCurvePoint(p=50, mult=Decimal("1.0")), SizingCurvePoint(p=95, mult=Decimal("2.5"))],
            min_target_size_percentile=Decimal("60"), min_order_usd=Decimal("5"),
            max_position_usd=Decimal("75"), max_concurrent_positions=8, reserve_pct=Decimal("20"),
        ),
    )


def rules(*raw):
    return [MatchRule.model_validate(r) for r in raw]


def test_deny_skip_names_the_rule_that_fired():
    skip = skip_for(SelectorsConfig(deny=rules({"asset": "sol"})))
    assert _skip_label(skip.reason) == "Excluded by deny rule"
    assert _skip_summary(skip.reason, skip.detail) == "asset=sol"


def test_deny_skip_renders_every_key_of_a_compound_rule():
    skip = skip_for(SelectorsConfig(deny=rules({"tag": "crypto-prices", "duration": "5m"})))
    assert _skip_summary(skip.reason, skip.detail) == "tag=crypto-prices duration=5m"


def test_allow_skip_leads_with_what_the_market_actually_was():
    """Nothing fired, so naming a rule can't explain the skip — the market's own
    identity is the answer to "why didn't my allowlist match"."""
    skip = skip_for(SelectorsConfig(allow=rules({"asset": "btc"}, {"asset": "xrp"})))
    assert _skip_label(skip.reason) == "Outside allowlist"
    assert _skip_summary(skip.reason, skip.detail) == "asset=sol matched none of 2 allow rules"


def test_allow_skip_singular_rule_reads_correctly():
    skip = skip_for(SelectorsConfig(allow=rules({"asset": "btc"})))
    assert _skip_summary(skip.reason, skip.detail).endswith("none of 1 allow rule")


def test_allow_skip_on_a_market_with_no_attributes_at_all():
    """A metadata gap and a genuine mismatch produce the same skip reason, so the
    summary has to distinguish them or the dashboard sends you hunting for a rule bug
    that isn't there."""
    skip = skip_for(SelectorsConfig(allow=rules({"asset": "btc"})), attrs={})
    assert _skip_summary(skip.reason, skip.detail).startswith("market with no identifying attributes")


def test_both_reasons_are_registered_everywhere():
    """A reason missing from SKIP_REASONS or _SKIP_REASON_META renders as a raw token
    in a default-colored pill — legible enough to survive review, wrong enough to
    look broken in the fleet view."""
    for reason in ("selector_deny", "selector_allow"):
        assert reason in SKIP_REASONS
        assert _skip_label(reason) != reason, f"{reason} has no label"
        assert _skip_category(reason) == "filtered"


def test_detail_survives_the_jsonb_round_trip():
    """decide() builds detail from frozensets and MatchRule objects; the dashboard
    reads it back out of a JSONB column. Anything not JSON-native would be lost
    between the two."""
    skip = skip_for(SelectorsConfig(deny=rules({"asset": "sol"})))
    revived = json.loads(json.dumps(skip.detail))
    assert revived == skip.detail
    assert _skip_summary(skip.reason, revived) == "asset=sol"


@pytest.mark.parametrize("selectors", [
    SelectorsConfig(deny=[MatchRule.model_validate({"asset": "sol"})]),
    SelectorsConfig(allow=[MatchRule.model_validate({"asset": "btc"})]),
])
def test_market_attrs_are_carried_for_debugging(selectors):
    skip = skip_for(selectors)
    assert skip.detail["market_attrs"]["series"] == ["sol-up-or-down-5m"]
