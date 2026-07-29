"""target_size_percentile sizing (FR-P-3, FR-P-6, FR-P-11). Pure functions only —
`policy/engine.py` is the only caller, and it must stay pure per FR-P-1.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from pmex_shadow.config import SizingCurvePoint
from pmex_shadow.models import Position, TargetFill, TargetPolicyStats

# Whole shares: the design doc's worked example floors 79.36 -> 79, not 79.360000 —
# FR-P-6's "round down" is to the nearest whole share, applied uniformly to both the
# entry (USD->shares) and exit (proportional) paths.
_SHARE_QUANT = Decimal("1")


def percentile_of_value(value: Decimal, target: TargetPolicyStats) -> Decimal:
    """Locate `value` in the target's observed size distribution (FR-P-3: "locate the
    fill's notional in the target's size distribution"). Interpolated in **sqrt-value
    space**, not linear or log value — reverse-engineered from the design doc's worked
    example (§3.3: $10k against p50=$2k/p80=$6k/p95=$15k → p88 → 2.0x → $50 → 79
    shares) to find the one interpolation method that reproduces it exactly, down to
    the "$49.77" parenthetical (79 shares × $0.63): sqrt-space interpolation gives
    percentile=87.51 (→"p88"), multiplier=2.0007 (→"2.0x"), sized=$50.02 (→"$50"),
    79 shares, $49.77 notional — matching to the displayed precision. Linear-in-value
    gives p86.7; log-value gives p88.4; neither reproduces the exact share count.
    Dampens outlier influence between linear and log, which is presumably why whoever
    wrote that example chose it, even though the design doc doesn't say so explicitly.
    """
    breakpoints = [
        (Decimal(0), Decimal(0)),
        (Decimal(50), target.size_p50),
        (Decimal(60), target.size_p60),
        (Decimal(80), target.size_p80),
        (Decimal(95), target.size_p95),
    ]
    if value <= 0:
        return Decimal(0)

    sqrt_value = value.sqrt()
    for (p_lo, v_lo), (p_hi, v_hi) in zip(breakpoints, breakpoints[1:]):
        if value <= v_hi:
            sqrt_lo, sqrt_hi = v_lo.sqrt(), v_hi.sqrt()
            if sqrt_hi <= sqrt_lo:
                return p_hi
            frac = (sqrt_value - sqrt_lo) / (sqrt_hi - sqrt_lo)
            return p_lo + frac * (p_hi - p_lo)

    p_lo, v_lo = breakpoints[-2]
    p_hi, v_hi = breakpoints[-1]
    sqrt_lo, sqrt_hi = v_lo.sqrt(), v_hi.sqrt()
    if sqrt_hi <= sqrt_lo:
        return Decimal(99)
    slope = (p_hi - p_lo) / (sqrt_hi - sqrt_lo)
    return min(p_hi + slope * (sqrt_value - sqrt_hi), Decimal(99))


def multiplier_for_percentile(percentile: Decimal, curve: list[SizingCurvePoint]) -> Decimal:
    """Interpolate the size multiplier from the configured curve (FR-P-3). Clamps to
    the curve's edge multipliers rather than extrapolating beyond configured points —
    an operator who configured a curve up to p95 didn't necessarily mean to imply
    what happens at p99.
    """
    points = sorted(curve, key=lambda c: c.p)
    if percentile <= points[0].p:
        return points[0].mult
    if percentile >= points[-1].p:
        return points[-1].mult
    for lo, hi in zip(points, points[1:]):
        if percentile <= hi.p:
            if hi.p == lo.p:
                return hi.mult
            frac = (percentile - lo.p) / (hi.p - lo.p)
            return lo.mult + frac * (hi.mult - lo.mult)
    return points[-1].mult


def shares_from_usd(usd: Decimal, reference_price: Decimal, min_order_usd: Decimal) -> tuple[Decimal, Decimal] | None:
    """FR-P-6: convert USD to shares at the reference price, rounding DOWN — never up.
    Returns (shares, actual_notional) or None if the rounded notional falls below
    `min_order_usd` (a skip, not a clamp up past the floor).
    """
    if reference_price <= 0 or usd <= 0:
        return None
    shares = (usd / reference_price).quantize(_SHARE_QUANT, rounding=ROUND_DOWN)
    if shares <= 0:
        return None
    notional = shares * reference_price
    if notional < min_order_usd:
        return None
    return shares, notional


def exit_shares(fill: TargetFill, target: TargetPolicyStats, our_position: Position | None) -> Decimal:
    """FR-P-11: sell the same *fraction* of our position that the target sold of
    theirs — never mirror their absolute share count. `target.position_before` is the
    target's own share count in this token immediately before this fill (see
    TargetPolicyStats docstring for why that's a caller-computed extension, not a
    `target_stats` column).
    """
    if our_position is None or our_position.shares <= 0:
        return Decimal(0)
    if target.position_before <= 0:
        return Decimal(0)
    fraction = min(fill.size / target.position_before, Decimal(1))
    return (our_position.shares * fraction).quantize(_SHARE_QUANT, rounding=ROUND_DOWN)
