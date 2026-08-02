"""market_attrs() against real captured Gamma payloads.

The fixture holds two families whose useful discriminator lives in completely
different places -- a crypto up/down market (structural `cryptoMarketConfig`) and a
daily weather market (no such block; series + geography tags on the event). Testing
both is the point: it's what keeps the extractor from quietly becoming crypto-shaped.

Regenerate with the snippet in the fixture-capture section of README, not by hand --
these are verbatim API responses, and editing them to make a test pass defeats the
reason they're checked in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pmex_shadow.market.cache import market_attrs

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "real_gamma_market_meta.json").read_text())


def attrs_for(family: str) -> dict[str, frozenset[str]]:
    f = FIXTURE[family]
    return market_attrs(f["market"], f["event"])


def test_crypto_market_exposes_underlying_asset():
    """The case this whole mechanism exists for: BTC/SOL/XRP 5m markets share a
    category and mint a new event every recurrence, so `asset` is the only stable
    thing that separates them."""
    attrs = attrs_for("crypto_5m")
    assert attrs["asset"] == frozenset({"sol"})
    assert attrs["duration"] == frozenset({"5m"})
    assert attrs["series"] == frozenset({"sol-up-or-down-5m"})


def test_weather_market_has_no_asset_but_still_resolves_identity():
    """Weather has no cryptoMarketConfig at all. Series and geography tags carry the
    identity instead -- same matcher, different keys, no code change."""
    attrs = attrs_for("weather_daily")
    assert "asset" not in attrs
    assert "duration" not in attrs
    assert attrs["series"] == frozenset({"hong-kong-daily-weather"})
    assert "hong-kong" in attrs["tag"]
    assert "weather" in attrs["tag"]


@pytest.mark.parametrize("family", ["crypto_5m", "weather_daily"])
def test_every_family_carries_the_common_keys(family):
    attrs = attrs_for(family)
    for key in ("slug", "event_id", "tag", "series"):
        assert key in attrs, f"{family} missing {key}"


def test_tags_are_slugs_not_labels():
    """Config authors write what they can see in a URL. Gamma's labels ("New York
    City") and slugs ("new-york-city") differ, and mixing them silently produces
    rules that never match."""
    attrs = attrs_for("weather_daily")
    assert all(" " not in t for t in attrs["tag"])


@pytest.mark.parametrize("family", ["crypto_5m", "weather_daily"])
def test_values_are_case_folded(family):
    """Gamma's slugs are not consistently lowercase despite looking it — the live SOL
    5m event tags itself "5M". Rule literals fold at parse time, so anything left
    unfolded here is a rule that silently never fires. Caught in production, not by
    the earlier version of this test, which asserted the property against a fixture
    that happened to be all-lowercase."""
    for key, values in attrs_for(family).items():
        assert all(v == v.lower() for v in values), f"{family}.{key} not folded: {sorted(values)}"


def test_the_uppercase_tag_that_exposed_this_is_still_in_the_fixture():
    """Guards the guard: if a refresh of the fixture loses the mixed-case tag, the
    test above starts passing vacuously and the regression can walk back in."""
    raw = FIXTURE["crypto_5m"]["event"]["tags"]
    assert any(t["slug"] != t["slug"].lower() for t in raw), "fixture no longer covers mixed-case slugs"


def test_absent_attributes_are_omitted_not_empty():
    """match.py distinguishes absent from empty -- an empty frozenset would turn a
    fail-closed allow rule into a pass. Guard the invariant at the source."""
    attrs = market_attrs({"slug": "bare-market"}, None)
    assert attrs == {"slug": frozenset({"bare-market"})}
    assert all(v for v in attrs.values())


def test_missing_event_drops_event_scoped_attributes():
    """A transient failure on the event fetch must not fabricate identity. Dropping
    the keys makes allow-scoped bots skip rather than guess (FR-M-3)."""
    market = FIXTURE["crypto_5m"]["market"]
    attrs = market_attrs(market, None)
    assert attrs["asset"] == frozenset({"sol"})  # structural, survives
    for key in ("series", "tag", "event_id", "category"):
        assert key not in attrs
