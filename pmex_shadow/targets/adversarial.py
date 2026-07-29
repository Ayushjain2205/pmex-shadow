"""Reversal-rate adversarial-copying detection (FR-T-5): the fraction of a target's
fills followed by their OWN opposing fill on the same token within
`reversal_window_s`. Surfaced on the scorecard, never auto-acted on — a whale who
buys, lets copiers lift the book behind them, and sells into that flow looks exactly
like this in the data (design doc §3.6), but so does an honest trader who's just
quick to take profits. The number is a flag for a human, not a policy input.
"""

from __future__ import annotations

import asyncpg


async def compute_reversal_rate(conn: asyncpg.Connection, target: str, window_s: int, since_days: int = 30) -> float | None:
    rows = await conn.fetch(
        """
        SELECT token_id, side, block_ts
        FROM target_fills
        WHERE target = $1 AND block_ts >= now() - make_interval(days => $2)
        ORDER BY block_ts
        """,
        target, since_days,
    )
    if not rows:
        return None

    reversed_count = 0
    for i, fill in enumerate(rows):
        for later in rows[i + 1:]:
            elapsed = (later["block_ts"] - fill["block_ts"]).total_seconds()
            if elapsed > window_s:
                break
            if later["token_id"] == fill["token_id"] and later["side"] != fill["side"]:
                reversed_count += 1
                break

    return reversed_count / len(rows)
