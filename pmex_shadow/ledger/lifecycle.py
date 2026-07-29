"""Position lifecycle state machine (FR-L-2):

    open -> pending_resolution -> {resolved, disputed, voided}
    resolved -> redeemed
    voided -> refunded
    losing -> written_off

Pure transition logic — no I/O. Callers (reconcile.py, redeem.py) do the on-chain
reads and pass in already-fetched facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ResolutionFacts:
    """What reconcile.py/redeem.py learn on-chain about a condition, passed in as
    data rather than fetched inside these pure transition functions."""

    resolved: bool  # payoutDenominator != 0
    disputed: bool  # from the UMA adapter / Data API — market flagged as disputed
    voided: bool  # all payout numerators zero, or explicit void flag
    winning: bool | None  # None if not yet resolved; True/False once it is


def next_lifecycle(current: str, facts: ResolutionFacts) -> str:
    """Advance `current` given newly-observed facts. Returns `current` unchanged if
    nothing warrants a transition — never guesses past what the facts support.
    """
    if current == "open":
        if facts.voided:
            return "voided"
        if facts.disputed:
            return "disputed"
        if facts.resolved:
            return "resolved" if facts.winning else "pending_resolution"  # losing positions wait for redeem.py's write_off, not an immediate jump
        return "open"

    if current == "pending_resolution":
        if facts.voided:
            return "voided"
        if facts.disputed:
            return "disputed"
        if facts.resolved:
            return "resolved"
        return current

    if current == "disputed":
        if facts.voided:
            return "voided"
        if facts.resolved and not facts.disputed:
            return "resolved"
        return current

    return current  # resolved/redeemed/voided/refunded/written_off are terminal-ish, advanced only by redeem.py's own explicit writes


def should_write_off(facts: ResolutionFacts) -> bool:
    """FR-L-8: losing positions have nothing to redeem — write off without a
    transaction, never spend gas discovering this on-chain."""
    return facts.resolved and facts.winning is False


def capital_returned_on(lifecycle: str) -> bool:
    """FR-L-3: capital returns to the envelope ONLY on confirmed redemption, never
    on observed resolution — this is the one-line check that keeps a bot from sizing
    against money it doesn't have yet."""
    return lifecycle in ("redeemed", "refunded", "written_off")


def redeem_retry_allowed(dispute_age_days: int, redeem_retry_days: int) -> bool:
    """FR-L-9: disputed markets retry on backoff bounded by redeem_retry_days, not a
    tight retry loop — a dispute can sit for weeks."""
    return dispute_age_days <= redeem_retry_days
