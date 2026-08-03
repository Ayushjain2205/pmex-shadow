from pathlib import Path

import pytest

from pmex_shadow.config import BotConfig, ConfigError, SelectorsConfig, load_bot_config, load_policy_file


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def test_load_bot_config_happy_path(tmp_path):
    path = _write(
        tmp_path,
        "sports_bot1.yaml",
        """
        name: sports_bot1
        mode: paper
        wallet: { funder_env: SPORTS_BOT1_FUNDER, pk_env: SPORTS_BOT1_PK }
        selectors:
          categories: [sports]
          min_book_liquidity_usd: "500"
        targets: [whale1, whale2]
        policy: { profile: tight }
        risk: { envelope_usd: "1500" }
        """,
    )
    cfg = load_bot_config(path)
    assert cfg.name == "sports_bot1"
    assert cfg.selectors.categories == ["sports"]


def test_bot_name_must_match_filename(tmp_path):
    path = _write(
        tmp_path,
        "bot2.yaml",
        """
        name: sports_bot1
        wallet: { funder_env: X, pk_env: Y }
        targets: []
        policy: { profile: tight }
        risk: { envelope_usd: "100" }
        """,
    )
    with pytest.raises(ConfigError, match="does not match filename"):
        load_bot_config(path)


def test_float_rejected_for_decimal_discipline(tmp_path):
    path = _write(
        tmp_path,
        "bot3.yaml",
        """
        name: bot3
        wallet: { funder_env: X, pk_env: Y }
        targets: []
        policy: { profile: tight }
        risk: { envelope_usd: 100.50 }
        """,
    )
    with pytest.raises(ConfigError, match="float value"):
        load_bot_config(path)


def test_no_selectors_means_copy_everything(tmp_path):
    path = _write(
        tmp_path,
        "bot4.yaml",
        """
        name: bot4
        wallet: { funder_env: X, pk_env: Y }
        targets: [whale1]
        policy: { profile: loose }
        risk: { envelope_usd: "100" }
        """,
    )
    cfg = load_bot_config(path)
    assert cfg.selectors.categories is None
    assert cfg.selectors.min_book_liquidity_usd is None


def test_load_policy_file(tmp_path):
    path = _write(
        tmp_path,
        "policy.yaml",
        """
        profiles:
          tight:
            max_slippage_ticks: 1
            volatility_guard: { window_s: 5, max_ticks: 2 }
            max_fill_age_s: 5
            sizing:
              base_unit_usd: "10"
              curve: [{p: 50, mult: "1.0"}, {p: 95, mult: "2.5"}]
              min_target_size_percentile: "60"
              min_order_usd: "5"
              max_position_usd: "50"
              max_concurrent_positions: 8
              reserve_pct: "20"
        risk:
          global_max_exposure_usd: "5000"
          max_orders_per_minute: 30
          halt_on_reconcile_drift_usd: "100"
        targets:
          decay: { window_days: 30, min_hit_rate: "0.45", auto_pause: true }
          dormancy_days: 21
        exits:
          mirror_sells: true
          auto_redeem: true
          redeem_retry_days: 30
        """,
    )
    policy = load_policy_file(path)
    assert policy.profiles["tight"].sizing.max_concurrent_positions == 8


def test_fixed_usd_sizing_mode(tmp_path):
    path = _write(
        tmp_path,
        "policy.yaml",
        """
        profiles:
          flat:
            max_slippage_ticks: 1
            volatility_guard: { window_s: 5, max_ticks: 2 }
            max_fill_age_s: 5
            sizing:
              mode: fixed_usd
              fixed_usd: "50"
              min_target_size_percentile: "60"
              min_order_usd: "5"
              max_position_usd: "50"
              max_concurrent_positions: 8
              reserve_pct: "20"
        risk:
          global_max_exposure_usd: "5000"
          max_orders_per_minute: 30
          halt_on_reconcile_drift_usd: "100"
        targets:
          decay: { window_days: 30, min_hit_rate: "0.45", auto_pause: true }
          dormancy_days: 21
        exits:
          mirror_sells: true
          auto_redeem: true
          redeem_retry_days: 30
        """,
    )
    policy = load_policy_file(path)
    sizing = policy.profiles["flat"].sizing
    assert sizing.mode == "fixed_usd"
    assert sizing.fixed_usd == 50
    assert sizing.base_unit_usd is None
    assert sizing.curve is None


def test_fixed_usd_mode_requires_fixed_usd(tmp_path):
    path = _write(
        tmp_path,
        "policy.yaml",
        """
        profiles:
          flat:
            max_slippage_ticks: 1
            volatility_guard: { window_s: 5, max_ticks: 2 }
            max_fill_age_s: 5
            sizing:
              mode: fixed_usd
              min_target_size_percentile: "60"
              min_order_usd: "5"
              max_position_usd: "50"
              max_concurrent_positions: 8
              reserve_pct: "20"
        risk:
          global_max_exposure_usd: "5000"
          max_orders_per_minute: 30
          halt_on_reconcile_drift_usd: "100"
        targets:
          decay: { window_days: 30, min_hit_rate: "0.45", auto_pause: true }
          dormancy_days: 21
        exits:
          mirror_sells: true
          auto_redeem: true
          redeem_retry_days: 30
        """,
    )
    with pytest.raises(Exception, match="fixed_usd"):
        load_policy_file(path)


def test_target_size_percentile_mode_requires_curve(tmp_path):
    path = _write(
        tmp_path,
        "policy.yaml",
        """
        profiles:
          tight:
            max_slippage_ticks: 1
            volatility_guard: { window_s: 5, max_ticks: 2 }
            max_fill_age_s: 5
            sizing:
              base_unit_usd: "10"
              min_target_size_percentile: "60"
              min_order_usd: "5"
              max_position_usd: "50"
              max_concurrent_positions: 8
              reserve_pct: "20"
        risk:
          global_max_exposure_usd: "5000"
          max_orders_per_minute: 30
          halt_on_reconcile_drift_usd: "100"
        targets:
          decay: { window_days: 30, min_hit_rate: "0.45", auto_pause: true }
          dormancy_days: 21
        exits:
          mirror_sells: true
          auto_redeem: true
          redeem_retry_days: 30
        """,
    )
    with pytest.raises(Exception, match="curve"):
        load_policy_file(path)


def test_null_event_ids_still_loads():
    """Active bot_config rows serialize event_ids explicitly as null. If those stop
    validating, consumer.py's hot-reload logs and keeps the *previous* config, so a
    bot silently stops honouring dashboard edits — a much quieter failure than a
    crash. Accepting null is what keeps that from happening."""
    assert SelectorsConfig.model_validate({"event_ids": None}).event_ids is None


def test_setting_event_ids_is_a_load_error():
    """It never filtered anything. Leaving it merely ignored would preserve the
    original bug — believing you'd scoped a bot when you hadn't."""
    with pytest.raises(Exception, match="never been implemented"):
        SelectorsConfig.model_validate({"event_ids": ["788072"]})


def test_match_rules_round_trip_through_model_dump():
    """A validated BotConfig is model_dump()ed straight into the bot_config table
    (config_write.seed_initial_config) and re-validated on every start and
    hot-reload. Without an explicit serializer pydantic emits MatchValue's own
    fields — {'pattern': None, 'literals': [...]} — which then fails validation as a
    shape no config author ever wrote, so a bot with any rule cannot start. Caught by
    a crash-looping container, not by the suite."""
    raw = {
        "name": "rt", "mode": "paper", "wallet": {"funder_env": "F", "pk_env": "P"},
        "selectors": {"deny": [{"asset": "sol"}, {"tag": ["a", "b"]}, {"slug": {"re": "sol-.*"}}]},
        "targets": ["t"], "policy": {"profile": "tight"}, "risk": {"envelope_usd": "500"},
    }
    dumped = BotConfig.model_validate(raw).model_dump(mode="json")
    assert dumped["selectors"]["deny"] == [{"asset": "sol"}, {"tag": ["a", "b"]}, {"slug": {"re": "sol-.*"}}]

    # Must survive re-validation, and dumping again must be stable — a bot re-dumps
    # its own config on every version write, so drift would compound.
    assert BotConfig.model_validate(dumped).model_dump(mode="json") == dumped
