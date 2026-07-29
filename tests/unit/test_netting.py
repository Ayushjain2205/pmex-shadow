import datetime as dt
from decimal import Decimal

from pmex_shadow.models import Intent, Side, Skip, TargetFill
from pmex_shadow.policy.netting import net_intents

NOW = dt.datetime(2026, 7, 29, tzinfo=dt.timezone.utc)


def make_fill(**overrides) -> TargetFill:
    defaults = dict(
        dedupe_key="tx:0", target="0xtarget", token_id="tok1", side=Side.BUY,
        price=Decimal("0.5"), size=Decimal("10"), notional_usd=Decimal("5"),
        block_number=1, block_ts=NOW, detected_at=NOW, source="chain",
    )
    defaults.update(overrides)
    return TargetFill(**defaults)


def make_intent(**overrides) -> Intent:
    defaults = dict(
        bot_id="bot1", fill=make_fill(), token_id="tok1", side=Side.BUY,
        limit_price=Decimal("0.51"), shares=Decimal("10"), notional_usd=Decimal("5.1"),
        target_percentile=Decimal("80"), size_multiplier=Decimal("1.5"),
    )
    defaults.update(overrides)
    return Intent(**defaults)


def test_opposing_intents_fully_cancel_and_net_out():
    buy = make_intent(side=Side.BUY, shares=Decimal("10"))
    sell = make_intent(side=Side.SELL, shares=Decimal("10"))
    result = net_intents([buy, sell])

    assert len(result) == 2
    assert all(isinstance(r, Skip) and r.reason == "netted_out" for r in result)


def test_non_opposing_intents_merge_to_net_quantity():
    buy1 = make_intent(side=Side.BUY, shares=Decimal("10"))
    buy2 = make_intent(side=Side.BUY, shares=Decimal("5"))
    result = net_intents([buy1, buy2])

    intents = [r for r in result if isinstance(r, Intent)]
    skips = [r for r in result if isinstance(r, Skip)]
    assert len(intents) == 1
    assert intents[0].shares == Decimal("15")
    assert intents[0].side == Side.BUY
    assert len(skips) == 1
    assert skips[0].reason == "netted_out"


def test_partial_offset_nets_to_remaining_side():
    buy = make_intent(side=Side.BUY, shares=Decimal("10"))
    sell = make_intent(side=Side.SELL, shares=Decimal("4"))
    result = net_intents([buy, sell])

    intents = [r for r in result if isinstance(r, Intent)]
    assert len(intents) == 1
    assert intents[0].side == Side.BUY
    assert intents[0].shares == Decimal("6")


def test_different_tokens_pass_through_independently():
    a = make_intent(token_id="tok1", side=Side.BUY, shares=Decimal("10"))
    b = make_intent(token_id="tok2", side=Side.BUY, shares=Decimal("5"))
    result = net_intents([a, b])

    assert len(result) == 2
    assert all(isinstance(r, Intent) for r in result)


def test_different_bots_do_not_net_against_each_other():
    a = make_intent(bot_id="bot1", side=Side.BUY, shares=Decimal("10"))
    b = make_intent(bot_id="bot2", side=Side.SELL, shares=Decimal("10"))
    result = net_intents([a, b])

    assert len(result) == 2
    assert all(isinstance(r, Intent) for r in result)


def test_skips_pass_through_untouched():
    s = Skip(bot_id="bot1", fill=make_fill(), reason="stale_fill")
    result = net_intents([s])
    assert result == [s]
