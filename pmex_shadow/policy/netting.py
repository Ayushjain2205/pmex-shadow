"""Within-bot netting of opposing intents on the same token (FR-P-10). Operates on a
*batch* of already-decided Intents (typically all produced within one processing
tick), unlike `decide()` which handles one fill at a time — netting is inherently a
cross-fill concern: "two targets in your sports bot can still take opposite sides of
the same game" (design doc §3.3), which only becomes visible once you have more than
one intent for the same (bot_id, token_id) in hand.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from decimal import Decimal

from pmex_shadow.models import Decision, Intent, Side, Skip


def net_intents(decisions: list[Decision]) -> list[Decision]:
    """Collapse opposing same-token Intents within a bot. Skips (already-rejected
    fills) pass through untouched — there's nothing to net against a non-decision.

    If the net quantity is zero, every contributing Intent becomes a `netted_out`
    Skip — you'd otherwise pay two spreads for zero resulting exposure. If nonzero,
    a single merged Intent is emitted for the net side/quantity (priced and fielded
    from the last-arriving intent in the group, since that's the most recent market
    read); the rest become `netted_out` Skips so nothing is double-counted downstream.
    """
    intents_by_key: dict[tuple[str, str], list[Intent]] = defaultdict(list)
    passthrough: list[Decision] = []

    for d in decisions:
        if isinstance(d, Skip):
            passthrough.append(d)
        else:
            intents_by_key[(d.bot_id, d.token_id)].append(d)

    result: list[Decision] = list(passthrough)
    for (bot_id, _token_id), group in intents_by_key.items():
        if len(group) == 1:
            result.append(group[0])
            continue

        net_shares = sum((g.shares if g.side == Side.BUY else -g.shares) for g in group)
        if net_shares == 0:
            result.extend(Skip(bot_id=g.bot_id, fill=g.fill, reason="netted_out") for g in group)
            continue

        keeper = group[-1]
        net_side = Side.BUY if net_shares > 0 else Side.SELL
        net_abs = abs(net_shares)
        merged = replace(
            keeper,
            side=net_side,
            shares=net_abs,
            notional_usd=net_abs * keeper.limit_price,
        )
        result.append(merged)
        result.extend(
            Skip(bot_id=g.bot_id, fill=g.fill, reason="netted_out") for g in group if g is not keeper
        )

    return result
