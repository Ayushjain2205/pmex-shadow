"""A target wallet's own Polymarket profile, read live from the Data API.

This is deliberately *not* sourced from our `target_fills` table. That table starts
the day we began watching a wallet and holds only what our two ingest paths caught;
the wallet's actual trading history predates it and is wider. For the question this
page exists to answer — "who is this wallet and is it worth following" — their real
history is the ground truth and ours is the sample.

Everything here is read-only, unauthenticated, and stored nowhere: the same public
API `watcher/sweep.py` already polls for fills, hit with different paths. Notably
`/positions` and `/activity` return the market `title` inline, so none of this needs
Gamma's per-token question lookup (control/queries.py's `get_market_titles`) — that
call exists because the *intents* table only records a token_id, not because market
titles are inherently expensive to get.

`user-pnl-api.polymarket.com/user-pnl` is deliberately not called. It looks like the
series behind the profile page's P&L chart, but its numbers don't reconcile with the
other endpoints: a wallet here with $235 of lifetime volume (`/traded`) reports a
series ending at -11,627, which is not a possible P&L on $235 traded. Until that
scale is understood it would be a wrong number rendered confidently, so the chart is
omitted rather than guessed at.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import logging
import re

import httpx

log = logging.getLogger(__name__)

# Recurring markets carry their period's unix start in the slug —
# `btc-updown-5m-1785853200`. Stripping it yields the series the wallet actually
# trades, which is the only level at which "which markets is this wallet in" has an
# answer: one target here shows 7,556 distinct tokens that collapse to 16 series.
# 9+ digits so a genuine slug ending in a year or a small number ("...-2026") isn't
# mistaken for a timestamp.
_PERIOD_SUFFIX = re.compile(r"-\d{9,}$")

# Recurring titles read "Bitcoin Up or Down - August 4, 10:20AM-10:25AM ET"; the part
# before the first " - " is the human name of the series.
def _series_key(slug: str | None) -> str | None:
    return _PERIOD_SUFFIX.sub("", slug) if slug else None


def _series_label(title: str | None) -> str:
    return (title or "").split(" - ")[0].strip() or "—"

# The wallet-facing Data API paths, all keyed by `user=<address>`.
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Gamma caps `clob_token_ids` at 100 per request; these wallet endpoints have no
# documented cap, but a profile page never needs more than a screenful.
POSITIONS_LIMIT = 50
ACTIVITY_PAGE_SIZE = 25

# A wider read of the same feed, fetched once and used for both the "what they trade"
# rollup and the per-market P&L ledger. Four pages rather than one because a ledger
# needs a market's *whole* lifecycle — the buys and the redemption that settles them —
# inside the window, and this wallet churns ~500 actions in two hours.
LEDGER_PAGE = 500
LEDGER_PAGES = 4

# Activity types that are wallet-level income with no market attached: a TAKER_REBATE
# row carries an empty conditionId and title, so it can't sit in a per-market ledger
# but must not be silently dropped from what the wallet earned either.
INCOME_TYPES = frozenset({"TAKER_REBATE", "MAKER_REBATE", "REWARD"})

# A market whose earliest activity sits right at the edge of the fetched window is
# probably clipped — we're seeing its redemption but not all of the buying that paid
# for it, which would report a wildly wrong profit. Those rows are shown but excluded
# from the totals.
EDGE_MARGIN_S = 120

# How many ledger rows to render. The window holds ~565 markets for a wallet trading
# 5-minute crypto, which is both a 400KB page and an unreadable wall; the totals are
# still computed over every one of them, and the series rollup above is the view that
# actually summarises all of it.
LEDGER_DISPLAY_ROWS = 150

# A losing market emits no REDEEM row at all — there is nothing to redeem when your
# side settles at zero — so "has a redemption" selects winners by construction and
# reports every wallet at a ~100% win rate. A market bought this long ago with no
# payout is treated as a total loss instead. Verified: applying it takes one wallet
# from 100% to 74% and another from 100% to 95%.
RESOLVED_AFTER_S = 3600

# Below this share of accountable markets the aggregate is not reported at all. The
# window is a *recent* slice, and for a wallet that trades rarely most of its markets
# straddle the edge — one such wallet derived +44% ROI while holding a $0 portfolio.
# Per-market rows stay correct and visible; only the headline is withheld.
MIN_COVERAGE = 0.80


async def _fetch_window(client: httpx.AsyncClient, base: str, addr: str) -> list[dict]:
    """The recent activity window, deduplicated.

    Offset paging over a live feed genuinely overlaps — this wallet trades several
    times a second, so rows shift between the concurrent requests and 2,000 fetched
    rows come back as ~1,978 unique. Dedupe is on the transaction hash plus the
    fields that distinguish two legs of the same transaction, not on the hash alone.
    """
    pages = await asyncio.gather(*(
        _get_json(client, f"{base}/activity", {"user": addr, "limit": LEDGER_PAGE, "offset": i * LEDGER_PAGE})
        for i in range(LEDGER_PAGES)
    ), return_exceptions=True)

    seen: set = set()
    rows: list[dict] = []
    for page in pages:
        if isinstance(page, Exception):
            log.warning("data-api activity window page failed for %s: %s", addr, page)
            continue
        for a in page:
            key = (a.get("transactionHash"), a.get("asset"), a.get("timestamp"), a.get("usdcSize"), a.get("type"))
            if key not in seen:
                seen.add(key)
                rows.append(a)
    rows.sort(key=lambda a: a.get("timestamp") or 0, reverse=True)
    return rows


def build_ledger(activity: list[dict], open_values: dict[str, float] | None = None) -> dict:
    """Every market the wallet traded in the window, with what it made or lost on each.

    Derived rather than fetched: `/positions` only returns what the wallet *currently*
    holds (15 rows for a wallet with thousands of trades — a redeemed position stops
    existing there), so a P&L history has to be reconstructed from the activity feed.
    For each market: what they paid in (buys less sells) against what came back out
    (the redemption). Verified against real data — 524 settled markets in one window
    reconciled to $201,091 in and $205,129 out.

    Markets clipped by the window edge are flagged and excluded from the totals: a
    redemption whose buys fall outside the window would otherwise read as pure profit.
    """
    open_values = open_values or {}
    stamps = [a["timestamp"] for a in activity if a.get("timestamp")]
    window_start = min(stamps) if stamps else 0
    latest = max(stamps) if stamps else 0

    markets: dict[str, dict] = {}
    income = 0.0
    for a in activity:
        if a.get("type") in INCOME_TYPES:
            income += a.get("usdcSize") or 0.0
            continue
        cid = a.get("conditionId")
        if not cid:
            continue
        m = markets.setdefault(cid, {
            "condition_id": cid, "title": a.get("title"), "slug": a.get("slug"),
            "series": _series_key(a.get("slug")), "outcome": a.get("outcome"),
            "trades": 0, "bought": 0.0, "sold": 0.0, "redeemed": 0.0,
            "first_ts": a.get("timestamp") or 0, "last_ts": a.get("timestamp") or 0,
        })
        ts = a.get("timestamp") or 0
        m["first_ts"] = min(m["first_ts"], ts)
        m["last_ts"] = max(m["last_ts"], ts)
        usd = a.get("usdcSize") or 0.0
        if a.get("type") == "REDEEM":
            m["redeemed"] += usd
        elif a.get("side") == "BUY":
            m["bought"] += usd
            m["trades"] += 1
        elif a.get("side") == "SELL":
            m["sold"] += usd
            m["trades"] += 1

    rows = []
    for m in markets.values():
        m["cost"] = m["bought"] - m["sold"]
        # Two ways a market's lifecycle can be only partly inside the window, both of
        # which turn a redemption into fictitious profit:
        #   - it sits at the window's edge, so some buying is off-screen; or
        #   - its buying predates the window entirely, leaving a redemption with no
        #     cost at all. A sparse wallet whose window spans 787 hours reported a
        #     100% win rate and double its money that way, which is what caught this.
        m["clipped"] = (
            m["first_ts"] <= window_start + EDGE_MARGIN_S
            or (m["redeemed"] > 0 and m["cost"] <= 0)
        )
        if m["condition_id"] in open_values:
            m["status"] = "open"
            m["value"] = open_values[m["condition_id"]]
            m["pnl"] = m["redeemed"] + m["value"] - m["cost"]
        elif m["redeemed"] > 0:
            m["status"] = "won"
            m["value"] = None
            m["pnl"] = m["redeemed"] - m["cost"]
        elif m["cost"] > 0 and (latest - m["last_ts"]) > RESOLVED_AFTER_S:
            # Long resolved and nothing ever came back: the position settled at zero.
            m["status"] = "lost"
            m["value"] = None
            m["pnl"] = -m["cost"]
        else:
            # Bought too recently to tell — the market may still be running.
            m["status"] = "pending"
            m["value"] = None
            m["pnl"] = None
        m["roi"] = (m["pnl"] / m["cost"]) if (m["pnl"] is not None and m["cost"]) else None
        m["last_at"] = dt.datetime.fromtimestamp(m["last_ts"], tz=dt.timezone.utc) if m["last_ts"] else None
        rows.append(m)

    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    counted = [r for r in rows if r["status"] in ("won", "lost") and not r["clipped"]]
    total_cost = sum(r["cost"] for r in counted)
    total_pnl = sum(r["pnl"] for r in counted)

    # What fraction of the markets they put money into can we actually account for?
    # Everything else is a market whose lifecycle straddles the window, and a headline
    # built on a biased slice of them is worse than no headline.
    accountable = [r for r in rows if r["cost"] > 0 or r["redeemed"] > 0]
    coverage = (len(counted) / len(accountable)) if accountable else 0.0
    return {
        "coverage": coverage,
        "reliable": coverage >= MIN_COVERAGE and len(counted) > 0,
        "excluded": len(accountable) - len(counted),
        # Totals below are computed over every market in the window; only the
        # rendered slice is capped.
        "rows": rows[:LEDGER_DISPLAY_ROWS],
        "total_markets": len(rows),
        "truncated": max(len(rows) - LEDGER_DISPLAY_ROWS, 0),
        "income": income,
        "settled": len(counted),
        "clipped": sum(1 for r in rows if r["clipped"]),
        "wins": sum(1 for r in counted if r["pnl"] > 0),
        "total_cost": total_cost,
        "total_payout": sum(r["redeemed"] for r in counted),
        "total_pnl": total_pnl,
        "roi": (total_pnl / total_cost) if total_cost else None,
        "win_rate": (sum(1 for r in counted if r["pnl"] > 0) / len(counted)) if counted else None,
        "window_hours": ((max(stamps) - window_start) / 3600) if len(stamps) > 1 else None,
    }


def summarise_behaviour(activity: list[dict]) -> dict:
    """Roll a raw activity feed into how this wallet actually trades.

    Everything here is derived from one sample of recent actions, not from all-time
    history — the page labels it as such. Redemptions are counted but kept out of the
    size statistics: a redemption's `usdcSize` is a payout, not a position they chose
    the size of, and blending the two makes a wallet look like it sizes far larger
    than it does.
    """
    trades = [a for a in activity if a.get("type") == "TRADE"]
    redeems = [a for a in activity if a.get("type") == "REDEEM"]
    # Rebates and rewards are neither: they'd otherwise vanish from the counts and
    # leave the panel's "N actions = X trades + Y redemptions" not adding up.
    rebates = [a for a in activity if a.get("type") in INCOME_TYPES]

    by_series: dict[str, dict] = {}
    for a in trades:
        key = _series_key(a.get("slug")) or "—"
        row = by_series.setdefault(key, {
            "series": key, "label": _series_label(a.get("title")),
            "trades": 0, "notional": 0.0, "icon": a.get("icon"),
        })
        row["trades"] += 1
        row["notional"] += a.get("usdcSize") or 0.0
    for row in by_series.values():
        row["avg_size"] = row["notional"] / row["trades"] if row["trades"] else 0.0
        row["share"] = row["trades"] / len(trades) if trades else 0.0
    series = sorted(by_series.values(), key=lambda r: r["trades"], reverse=True)

    sizes = sorted((a.get("usdcSize") or 0.0) for a in trades)
    sides = collections.Counter(a.get("side") for a in trades)

    span_s = None
    stamps = [a["timestamp"] for a in activity if a.get("timestamp")]
    if len(stamps) > 1:
        span_s = max(stamps) - min(stamps)

    return {
        "sampled": len(activity),
        "trades": len(trades),
        "redeems": len(redeems),
        "rebates": len(rebates),
        "buys": sides.get("BUY", 0),
        "sells": sides.get("SELL", 0),
        "series": series,
        "distinct_series": len(series),
        "median_size": sizes[len(sizes) // 2] if sizes else None,
        "avg_size": (sum(sizes) / len(sizes)) if sizes else None,
        "max_size": sizes[-1] if sizes else None,
        # Trades per hour over the sampled window — the honest cadence measure, since
        # the sample is "the last N actions" and its wall-clock span varies per wallet.
        "trades_per_hour": (len(trades) / (span_s / 3600)) if span_s else None,
        "span_hours": (span_s / 3600) if span_s else None,
    }


async def _get_json(client: httpx.AsyncClient, url: str, params: dict | list):
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


async def fetch_profile(
    data_api_base_url: str,
    address: str,
    activity_page: int = 1,
) -> dict:
    """Fetch the five wallet views the profile page renders, concurrently.

    Partial failure degrades rather than 500s: each endpoint is gathered with
    `return_exceptions=True` and a failed one comes back as None with its name in
    `errors`. The page is worth showing with three of five panels — the Postgres
    half (our own decisions about this wallet) doesn't depend on any of this and
    always renders.
    """
    base = data_api_base_url.rstrip("/")
    addr = address.lower()
    offset = max(activity_page - 1, 0) * ACTIVITY_PAGE_SIZE

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            # One extra row beyond the page size is the "is there a next page"
            # probe: these endpoints return no total count, so the only way to know
            # whether to render a Next link is to ask for one more than we show.
            _get_json(client, f"{base}/activity", {"user": addr, "limit": ACTIVITY_PAGE_SIZE + 1, "offset": offset}),
            _get_json(client, f"{base}/positions", {"user": addr, "limit": POSITIONS_LIMIT}),
            _get_json(client, f"{base}/value", {"user": addr}),
            # Identity (pseudonym, avatar, bio) isn't its own endpoint — it rides
            # along on each trade row, so one trade is the cheapest way to get it.
            _get_json(client, f"{base}/trades", {"user": addr, "limit": 1}),
            _fetch_window(client, base, addr),
            return_exceptions=True,
        )

    names = ("activity", "positions", "value", "identity", "window")
    data: dict = {}
    errors: list[str] = []
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            log.warning("data-api %s failed for %s: %s", name, addr, result)
            errors.append(name)
            data[name] = None
        else:
            data[name] = result

    activity = data["activity"] or []
    has_next = len(activity) > ACTIVITY_PAGE_SIZE
    activity = activity[:ACTIVITY_PAGE_SIZE]
    # Unix seconds here, but every timestamp in the templates is an ISO string that
    # base.html's script localises — converting in the template instead would mean a
    # custom Jinja filter for one caller.
    for row in activity:
        ts = row.get("timestamp")
        row["at"] = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc) if ts else None

    positions = data["positions"] or []
    # Open exposure first; a wallet like this one carries hundreds of settled-to-zero
    # rows that would otherwise bury everything it currently holds.
    positions.sort(key=lambda p: (p.get("currentValue") or 0), reverse=True)

    identity_row = (data["identity"] or [{}])
    identity = identity_row[0] if identity_row else {}

    value = data["value"]
    window = data["window"] or []
    # Still-held markets carry a live mark instead of a payout, so the ledger can show
    # an unrealized number for them rather than a blank.
    open_values = {p["conditionId"]: p.get("currentValue") or 0.0 for p in positions if p.get("conditionId")}

    return {
        "behaviour": summarise_behaviour(window),
        "ledger": build_ledger(window, open_values),
        "activity": activity,
        "activity_page": max(activity_page, 1),
        "activity_has_next": has_next,
        "activity_page_size": ACTIVITY_PAGE_SIZE,
        "positions": positions,
        "portfolio_value": (value[0]["value"] if isinstance(value, list) and value else None),
        "name": identity.get("name") or None,
        "pseudonym": identity.get("pseudonym") or None,
        "profile_image": identity.get("profileImage") or None,
        "bio": identity.get("bio") or None,
        "errors": errors,
    }
