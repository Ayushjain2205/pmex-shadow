"""Rule matching, with the cross-family cases that motivated the design.

The interesting behavior isn't "does {asset: sol} match a SOL market" — it's what
happens when a rule names an attribute the market doesn't have at all, which is the
normal case the moment one bot's config spans crypto and anything else.
"""

from __future__ import annotations

import pytest

from pmex_shadow.config import ConfigError, MatchRule
from pmex_shadow.policy.match import describe_attrs, first_denied, is_allowed, rule_matches

CRYPTO = {
    "asset": frozenset({"sol"}),
    "duration": frozenset({"5m"}),
    "series": frozenset({"sol-up-or-down-5m"}),
    "tag": frozenset({"crypto-prices", "up-or-down", "recurring"}),
    "slug": frozenset({"sol-updown-5m-1785792900"}),
}
WEATHER = {
    "series": frozenset({"nyc-daily-weather"}),
    "tag": frozenset({"weather", "new-york-city", "daily-temperature"}),
    "slug": frozenset({"highest-temperature-in-nyc-on-august-2-2026-74-75f"}),
}


def rule(raw) -> MatchRule:
    return MatchRule.model_validate(raw)


# --- rule_matches: keys AND, values OR ---


def test_all_keys_must_match():
    assert rule_matches(rule({"asset": "sol", "duration": "5m"}), CRYPTO)
    assert not rule_matches(rule({"asset": "sol", "duration": "15m"}), CRYPTO)


def test_values_within_a_key_are_or():
    assert rule_matches(rule({"asset": ["btc", "sol", "xrp"]}), CRYPTO)
    assert not rule_matches(rule({"asset": ["btc", "xrp"]}), CRYPTO)


def test_matches_any_member_of_a_multi_valued_attribute():
    """Tags are genuinely multi-valued — matching must be membership, not equality."""
    assert rule_matches(rule({"tag": "up-or-down"}), CRYPTO)
    assert rule_matches(rule({"tag": "recurring"}), CRYPTO)


def test_literal_matching_is_case_insensitive():
    assert rule_matches(rule({"asset": "SOL"}), CRYPTO)


# --- the absent-attribute asymmetry ---


def test_deny_is_inert_against_a_family_lacking_the_attribute():
    """A crypto deny rule must not silently exclude every weather market just because
    weather has no `asset`. This is the case that makes one bot config able to span
    families at all."""
    assert first_denied([rule({"asset": "sol"})], WEATHER) is None
    assert first_denied([rule({"asset": "sol"})], CRYPTO) is not None


def test_allow_fails_closed_against_a_family_lacking_the_attribute():
    """Same rule, opposite outcome: an allowlist admits only what it can positively
    identify, so a market with no `asset` is not admitted."""
    assert not is_allowed([rule({"asset": "sol"})], WEATHER)
    assert is_allowed([rule({"asset": "sol"})], CRYPTO)


def test_absent_attribute_never_passes_vacuously():
    assert not rule_matches(rule({"asset": "sol"}), {})
    assert not rule_matches(rule({"asset": "sol"}), {"asset": frozenset()})


# --- list semantics ---


def test_no_rules_configured_is_no_constraint():
    assert first_denied(None, CRYPTO) is None
    assert first_denied([], CRYPTO) is None
    assert is_allowed(None, CRYPTO)
    assert is_allowed([], CRYPTO)


def test_multiple_rules_are_or():
    rules = [rule({"asset": "btc"}), rule({"asset": "sol"})]
    assert first_denied(rules, CRYPTO) is not None
    assert is_allowed(rules, CRYPTO)


def test_first_denied_returns_the_rule_that_fired():
    rules = [rule({"asset": "btc"}), rule({"tag": "up-or-down"})]
    assert first_denied(rules, CRYPTO).describe() == {"tag": "up-or-down"}


# --- the regex escape hatch ---


def test_pattern_matches_a_family_with_no_structured_key():
    assert rule_matches(rule({"slug": {"re": r"highest-temperature-in-nyc-.*"}}), WEATHER)


def test_patterns_are_anchored():
    """`sol` must not match `solana...`; fullmatch is what keeps a short deny pattern
    from quietly swallowing unrelated markets."""
    assert not rule_matches(rule({"asset": {"re": "so"}}), CRYPTO)
    assert rule_matches(rule({"asset": {"re": "so.*"}}), CRYPTO)


def test_invalid_regex_is_rejected_at_config_load():
    with pytest.raises(Exception, match="invalid regex"):
        rule({"slug": {"re": "["}})


def test_unknown_attribute_is_rejected():
    """A typo'd deny key fails open — you keep copying what you meant to exclude —
    so it has to fail at load instead."""
    with pytest.raises(Exception, match="unknown attribute"):
        rule({"assett": "sol"})


def test_malformed_mapping_value_is_rejected():
    with pytest.raises(Exception, match="must be exactly"):
        rule({"asset": {"regex": "sol"}})


# --- skip detail ---


def test_describe_attrs_is_json_safe_and_sorted():
    described = describe_attrs(CRYPTO)
    assert described["tag"] == ["crypto-prices", "recurring", "up-or-down"]
    assert all(isinstance(v, list) for v in described.values())
