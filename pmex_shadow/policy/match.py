"""Identity rule matching over MarketMeta.attrs (see config.MatchRule).

Pure and dependency-light on purpose: `decide()` calls this for the authoritative
verdict, and execution/consumer.py calls the same `first_denied()` as a fast path to
avoid fetching an order book for a market it was always going to reject. Both must
agree, so there is exactly one implementation and neither owns it.

The deny/allow asymmetry people expect — "deny ignores markets that lack the
attribute, allow rejects them" — needs no special casing. One predicate does it:

    rule_matches := every key in the rule is present on the market AND matches

    deny  -> any rule matches            => skip. A rule keyed on an attribute this
                                            market doesn't have simply can't match,
                                            so it stays inert instead of denying
                                            every market in unrelated families.
    allow -> no rule matches             => skip. The same "can't match" outcome now
                                            means skip, which is the fail-closed
                                            reading: an allowlist admits only what
                                            it can positively identify.

That's what makes `{asset: sol}` behave sensibly against a weather market, which
carries no `asset` at all: inert as a deny, exclusionary as an allow.
"""

from __future__ import annotations

from typing import Mapping

from pmex_shadow.config import MatchRule


def rule_matches(rule: MatchRule, attrs: Mapping[str, frozenset[str]]) -> bool:
    """True when every attribute the rule names is present on the market and matches.

    Absent beats empty: `attrs.get(key)` returning None is a non-match, never a
    vacuous pass. market_attrs() upholds the other half by omitting keys rather than
    emitting empty sets.
    """
    for key, value in rule.root.items():
        present = attrs.get(key)
        if not present or not value.matches(present):
            return False
    return True


def first_denied(rules: list[MatchRule] | None, attrs: Mapping[str, frozenset[str]]) -> MatchRule | None:
    """The first deny rule this market trips, or None. First rather than all: the
    skip only needs to name one reason, and stopping early keeps the consumer's
    pre-book fast path cheap."""
    if not rules:
        return None
    for rule in rules:
        if rule_matches(rule, attrs):
            return rule
    return None


def is_allowed(rules: list[MatchRule] | None, attrs: Mapping[str, frozenset[str]]) -> bool:
    """True when no allowlist is configured (absent = no constraint, FR-P-2), or at
    least one rule matches."""
    if not rules:
        return True
    return any(rule_matches(rule, attrs) for rule in rules)


def describe_attrs(attrs: Mapping[str, frozenset[str]]) -> dict[str, list[str]]:
    """JSON-safe view of a market's attributes for Skip.detail.

    Worth carrying in full even though it's redundant with the rule: the common
    confusion is "why didn't my rule fire," and the answer is almost always that the
    market's actual attributes aren't what the author assumed. Putting them in the
    skip row means the dashboard can answer that without a re-fetch.
    """
    return {key: sorted(values) for key, values in sorted(attrs.items())}
