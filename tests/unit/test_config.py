from pathlib import Path

import pytest

from pmex_shadow.config import ConfigError, load_bot_config, load_policy_file


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
