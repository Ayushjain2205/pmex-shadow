from pmex_shadow.ledger.lifecycle import (
    ResolutionFacts,
    capital_returned_on,
    next_lifecycle,
    redeem_retry_allowed,
    should_write_off,
)


def test_open_stays_open_when_unresolved():
    facts = ResolutionFacts(resolved=False, disputed=False, voided=False, winning=None)
    assert next_lifecycle("open", facts) == "open"


def test_open_to_resolved_when_winning():
    facts = ResolutionFacts(resolved=True, disputed=False, voided=False, winning=True)
    assert next_lifecycle("open", facts) == "resolved"


def test_open_to_pending_resolution_when_losing():
    """Losing positions don't jump straight to a terminal state here -- redeem.py's
    should_write_off() makes that call explicitly, with its own transaction-free write."""
    facts = ResolutionFacts(resolved=True, disputed=False, voided=False, winning=False)
    assert next_lifecycle("open", facts) == "pending_resolution"


def test_open_to_disputed():
    facts = ResolutionFacts(resolved=False, disputed=True, voided=False, winning=None)
    assert next_lifecycle("open", facts) == "disputed"


def test_open_to_voided():
    facts = ResolutionFacts(resolved=False, disputed=False, voided=True, winning=None)
    assert next_lifecycle("open", facts) == "voided"


def test_disputed_resolves_once_dispute_clears():
    facts = ResolutionFacts(resolved=True, disputed=False, voided=False, winning=True)
    assert next_lifecycle("disputed", facts) == "resolved"


def test_terminal_states_never_move_backward():
    facts = ResolutionFacts(resolved=True, disputed=True, voided=True, winning=True)
    for terminal in ("redeemed", "refunded", "written_off"):
        assert next_lifecycle(terminal, facts) == terminal


def test_should_write_off_only_when_resolved_and_losing():
    assert should_write_off(ResolutionFacts(resolved=True, disputed=False, voided=False, winning=False))
    assert not should_write_off(ResolutionFacts(resolved=True, disputed=False, voided=False, winning=True))
    assert not should_write_off(ResolutionFacts(resolved=False, disputed=False, voided=False, winning=None))


def test_capital_returned_only_on_confirmed_terminal_states():
    assert capital_returned_on("redeemed")
    assert capital_returned_on("refunded")
    assert capital_returned_on("written_off")
    for non_terminal in ("open", "pending_resolution", "resolved", "disputed", "voided"):
        assert not capital_returned_on(non_terminal)


def test_redeem_retry_bounded_by_configured_days():
    assert redeem_retry_allowed(dispute_age_days=5, redeem_retry_days=30)
    assert not redeem_retry_allowed(dispute_age_days=31, redeem_retry_days=30)
