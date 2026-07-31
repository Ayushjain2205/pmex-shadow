"""Slippage, volatility, and staleness guards (FR-P-7..9). Pure functions — no clock
reads (`now` is always passed in), no I/O.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pmex_shadow.models import BookSnapshot, Side, TargetFill


def slippage_guard(fill: TargetFill, book: BookSnapshot, max_slippage_ticks: int, tick_size: Decimal) -> tuple[Decimal | None, dict]:
    """FR-P-7: skip if the best available price is worse than the target's own fill
    price by more than `max_slippage_ticks`. Directional — a price move *in our favor*
    since the target's fill never trips this guard, only an adverse one does.

    Returns (reference price to size against, detail) — reference price is None if
    the guard fails (no book, or slippage exceeds the limit); detail carries the
    actual numbers either way, for a skip's `Skip.detail`.
    """
    levels = book.asks if fill.side == Side.BUY else book.bids
    if not levels:
        return None, {"reason": "no_liquidity_on_book_side"}
    best_price = levels[0][0]
    adverse = (best_price - fill.price) if fill.side == Side.BUY else (fill.price - best_price)
    adverse_ticks = (adverse / tick_size) if adverse > 0 else Decimal(0)
    detail = {
        "target_price": str(fill.price), "best_price": str(best_price),
        "adverse_ticks": float(adverse_ticks), "max_slippage_ticks": max_slippage_ticks,
    }
    if adverse > 0 and adverse_ticks > max_slippage_ticks:
        return None, detail
    return best_price, detail


def _mid(book: BookSnapshot) -> Decimal | None:
    if not book.bids or not book.asks:
        return None
    return (book.bids[0][0] + book.asks[0][0]) / 2


def volatility_guard(
    book: BookSnapshot,
    book_history: tuple[BookSnapshot, ...],
    window_s: int,
    max_ticks: int,
    tick_size: Decimal,
    now: dt.datetime,
) -> tuple[bool, dict]:
    """FR-P-8: skip if the book has moved more than `max_ticks` within `window_s`.
    `book_history` is a caller-supplied rolling window of recent snapshots for this
    token — `decide()`'s own signature (§6) has room for only one `book` snapshot, so
    the caller (research/replay, execution) is responsible for maintaining this
    window and passing it in; `decide()` itself holds no state (FR-P-1).

    Returns (passed, detail) — passed is True if not too volatile (safe to proceed).
    """
    current_mid = _mid(book)
    if current_mid is None:
        return True, {"reason": "no_current_book_to_compare"}  # can't judge, don't block

    cutoff = now - dt.timedelta(seconds=window_s)
    moves: list[Decimal] = []
    for past in book_history:
        if past.taken_at < cutoff:
            continue
        past_mid = _mid(past)
        if past_mid is not None:
            moves.append(abs(current_mid - past_mid))

    if not moves:
        return True, {"reason": "no_history_in_window"}  # nothing to compare against yet

    move_ticks = max(moves) / tick_size
    detail = {"move_ticks": float(move_ticks), "max_ticks": max_ticks, "window_s": window_s}
    return move_ticks <= max_ticks, detail


def staleness_guard(fill: TargetFill, max_fill_age_s: int, now: dt.datetime) -> tuple[bool, dict]:
    """FR-P-9: skip if `now - block_ts` exceeds `max_fill_age_s`. Returns (passed,
    detail) — passed is True if the fill is still fresh enough to copy."""
    age_s = (now - fill.block_ts).total_seconds()
    detail = {"age_s": round(age_s, 1), "max_fill_age_s": max_fill_age_s}
    return age_s <= max_fill_age_s, detail
