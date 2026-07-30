import datetime as dt
from decimal import Decimal

from pmex_shadow.targets.decay import DecayCheckInput, check_decay

NOW = dt.datetime(2026, 7, 30, tzinfo=dt.timezone.utc)


def _base(**overrides) -> DecayCheckInput:
    defaults = dict(
        status="active", hit_rate_30d=Decimal("0.55"), fills_30d=50,
        last_fill_at=NOW - dt.timedelta(days=1), now=NOW,
        min_hit_rate=Decimal("0.45"), min_sample_size=20, dormancy_days=21,
    )
    defaults.update(overrides)
    return DecayCheckInput(**defaults)


def test_no_change_when_healthy():
    assert check_decay(_base()) is None


def test_pauses_on_low_hit_rate_with_sufficient_sample():
    inp = _base(hit_rate_30d=Decimal("0.30"), fills_30d=25)
    assert check_decay(inp) == "paused_decay"


def test_does_not_pause_on_low_hit_rate_with_insufficient_sample():
    """A target with only 5 fills at 20% hit rate could just be unlucky -- the
    sample-size floor exists precisely so noise doesn't get treated as decay."""
    inp = _base(hit_rate_30d=Decimal("0.20"), fills_30d=5)
    assert check_decay(inp) is None


def test_pauses_on_dormancy():
    inp = _base(last_fill_at=NOW - dt.timedelta(days=25))
    assert check_decay(inp) == "paused_dormant"


def test_dormancy_checked_before_decay_takes_priority_when_both_apply():
    inp = _base(last_fill_at=NOW - dt.timedelta(days=25), hit_rate_30d=Decimal("0.10"), fills_30d=50)
    assert check_decay(inp) == "paused_dormant"


def test_already_paused_targets_are_never_touched():
    for status in ("paused_decay", "paused_dormant", "paused_manual"):
        inp = _base(status=status, hit_rate_30d=Decimal("0.10"), fills_30d=100)
        assert check_decay(inp) is None


def test_no_fill_history_does_not_crash_or_falsely_flag_dormancy():
    inp = _base(last_fill_at=None)
    assert check_decay(inp) is None
