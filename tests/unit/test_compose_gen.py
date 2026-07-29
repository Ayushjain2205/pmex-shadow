from decimal import Decimal

from pmex_shadow.config import BotConfig, PolicyRef, RiskConfig, SelectorsConfig, WalletConfig
from pmex_shadow.ops.compose_gen import compute_overlaps, generate_compose


def make_bot(name, targets, categories=None, envelope="500") -> BotConfig:
    return BotConfig(
        name=name, mode="paper",
        wallet=WalletConfig(funder_env=f"{name.upper()}_FUNDER", pk_env=f"{name.upper()}_PK"),
        selectors=SelectorsConfig(categories=categories),
        targets=targets, policy=PolicyRef(profile="tight"),
        risk=RiskConfig(envelope_usd=Decimal(envelope)),
    )


def test_no_overlap_different_targets():
    a = make_bot("a", ["whale1"])
    b = make_bot("b", ["whale2"])
    assert compute_overlaps([a, b]) == []


def test_overlap_shared_target_no_selectors():
    """No category selector on either side means "copies everything" -- they
    trivially intersect."""
    a = make_bot("a", ["whale1", "whale2"])
    b = make_bot("b", ["whale1"])
    overlaps = compute_overlaps([a, b])
    assert len(overlaps) == 1
    assert overlaps[0]["bot_a"] == "a"
    assert overlaps[0]["bot_b"] == "b"
    assert overlaps[0]["shared_targets"] == ["whale1"]
    assert overlaps[0]["combined_exposure_usd"] == Decimal("1000")


def test_overlap_shared_target_intersecting_categories():
    a = make_bot("a", ["whale1"], categories=["sports", "politics"])
    b = make_bot("b", ["whale1"], categories=["politics"])
    assert len(compute_overlaps([a, b])) == 1


def test_no_overlap_shared_target_disjoint_categories():
    a = make_bot("a", ["whale1"], categories=["sports"])
    b = make_bot("b", ["whale1"], categories=["politics"])
    assert compute_overlaps([a, b]) == []


def test_generate_compose_produces_one_service_per_bot():
    bots = [make_bot("sports_bot1", ["whale1"]), make_bot("bot2", ["whale1"])]
    yaml_out = generate_compose(bots)
    assert "bot-sports_bot1" in yaml_out
    assert "bot-bot2" in yaml_out
    assert "GENERATED" in yaml_out
    assert "secrets/sports_bot1.env" in yaml_out
