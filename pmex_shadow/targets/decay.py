"""Auto-pause on hit-rate decay (FR-T-2) or dormancy (FR-T-3). Pure decision
function — the scheduled job (`run_decay_check`) does the DB read/write, this just
decides. Targets stop working for real reasons (they get copied to death, change
strategy, or were variance all along, design doc §3.6) — this is what cuts the
system's own losers without a human watching every scorecard.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class DecayCheckInput:
    status: str
    hit_rate_30d: Decimal | None
    fills_30d: int
    last_fill_at: dt.datetime | None
    now: dt.datetime
    min_hit_rate: Decimal
    min_sample_size: int
    dormancy_days: int


def check_decay(inp: DecayCheckInput) -> str | None:
    """Returns the new status if a pause is warranted, else None (no change).
    Never un-pauses — that's an operator action (`targets resume`), not automatic.
    """
    if inp.status not in ("shadow", "active"):
        return None  # already paused (any reason) or otherwise not eligible

    if inp.last_fill_at is not None:
        dormant_days = (inp.now - inp.last_fill_at).days
        if dormant_days >= inp.dormancy_days:
            return "paused_dormant"

    if inp.hit_rate_30d is not None and inp.fills_30d >= inp.min_sample_size and inp.hit_rate_30d < inp.min_hit_rate:
        return "paused_decay"

    return None
