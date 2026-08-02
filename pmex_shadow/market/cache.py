"""Token metadata + book fetch (FR-M-1..4). The book fetch shipped in Phase 1 (needed
by the paper logger); market metadata ships now that Phase 2's policy selectors
actually consume it. No persistent warm/refresh cache yet (FR-M-2) — that's still
Phase 2 scope but deferred past this first cut; every call here hits Gamma live.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import httpx

from pmex_shadow.models import BookSnapshot, MarketMeta, ResolutionStatus


async def get_orderbook(clob_base_url: str, token_id: str) -> BookSnapshot | None:
    """Fetch the live order book for a token. Returns None if the market has no book
    (e.g. not yet trading) — callers must treat that as "cannot simulate," not an error.

    The CLOB /book response returns each side sorted worst-price-first (confirmed
    empirically 2026-07-29: bids ascending, asks descending) — sort explicitly here
    rather than trust that ordering, since it isn't documented.
    """
    url = f"{clob_base_url.rstrip('/')}/book"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params={"token_id": token_id})
    if resp.status_code != 200:
        return None
    data = resp.json()
    if "error" in data:
        return None

    bids = sorted(
        ((Decimal(b["price"]), Decimal(b["size"])) for b in data.get("bids", [])),
        key=lambda t: t[0],
        reverse=True,
    )
    asks = sorted(
        ((Decimal(a["price"]), Decimal(a["size"])) for a in data.get("asks", [])),
        key=lambda t: t[0],
    )
    return BookSnapshot(
        token_id=token_id,
        bids=bids,
        asks=asks,
        taken_at=dt.datetime.now(dt.timezone.utc),
    )


def _category_from_event(event: dict | None) -> str | None:
    """The single `category` label (see get_market_meta's docstring for why it's the
    first non-"All" tag rather than the tag set). Superseded by the `tag` match
    attribute below, which keeps every tag — kept because existing bot configs and
    the control UI's params form both still write `selectors.categories`.
    """
    if event is None:
        return None
    tags = event.get("tags") or []
    non_generic = [t["label"] for t in tags if t.get("label") and t["label"].lower() != "all"]
    return non_generic[0] if non_generic else None


def market_attrs(market: dict, event: dict | None) -> dict[str, frozenset[str]]:
    """Build MarketMeta.attrs — the flat bag the deny/allow rules match against.

    Pure, and deliberately split from the fetch above so it can be tested against
    captured payloads (tests/fixtures/real_gamma_market_meta.json) rather than shapes
    invented from the docs. Adding a new market family should only ever touch here.

    A key is omitted entirely when the market has no such attribute, which is load
    bearing: match.py distinguishes "absent" from "present but non-matching," so
    emitting an empty frozenset instead of omitting would quietly turn a fail-closed
    allow rule into a pass. That also means a transient failure on the event fetch
    (event=None) drops `series`/`tag` and makes allow-scoped bots skip rather than
    guess, consistent with FR-M-3.

    Tag values are Gamma's `slug`s, not the `label`s `category` uses — slugs are
    lowercase, stable, and what shows up in URLs ("new-york-city", not "New York
    City"), so they're what a config author can actually predict.
    """
    attrs: dict[str, frozenset[str]] = {}

    slug = market.get("slug")
    if slug:
        attrs["slug"] = frozenset({slug})

    # Crypto up/down markets carry their underlying and cadence structurally, which is
    # the only reliable way to tell BTC from SOL from XRP within one series family --
    # they share a category, and their event ids change every recurrence.
    cfg = market.get("cryptoMarketConfig") or {}
    if cfg.get("asset"):
        attrs["asset"] = frozenset({str(cfg["asset"]).lower()})
    if cfg.get("duration"):
        attrs["duration"] = frozenset({str(cfg["duration"]).lower()})

    if event is not None:
        if event.get("id"):
            attrs["event_id"] = frozenset({str(event["id"])})
        series = {s["slug"] for s in (event.get("series") or []) if s.get("slug")}
        if series:
            attrs["series"] = frozenset(series)
        tags = {t["slug"] for t in (event.get("tags") or []) if t.get("slug")}
        if tags:
            attrs["tag"] = frozenset(tags)

    category = _category_from_event(event)
    if category:
        attrs["category"] = frozenset({category})

    return attrs


async def get_market_meta(gamma_api_base_url: str, token_id: str) -> MarketMeta | None:
    """Fetch category/tick-size/neg-risk/tradeable metadata for a token from Gamma
    (docs/VERIFIED.md item 6). Returns None on a cache miss (FR-M-3: a scoped bot
    must skip and fetch async, never guess — this is the "never guess" half; the
    caller decides what "skip" means for its context).

    `category` is a single label, not the full tag set Gamma actually returns
    (`tags: [{label, ...}]`) — Gamma's dedicated `category` field is consistently
    null in live data (confirmed while building this; the real signal lives in
    `tags`), so this picks the first tag that isn't the generic "All" bucket. A bot's
    `categories` selector is a single-label match against that choice, not a
    multi-tag search — simplest reading of the design doc's `categories: [sports]`
    example, and documented here as a Phase 2 design decision, not a protocol fact.
    """
    url = f"{gamma_api_base_url.rstrip('/')}/markets"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params={"clob_token_ids": token_id})
    if resp.status_code != 200:
        return None
    markets = resp.json()
    if not markets:
        return None
    m = markets[0]

    events = m.get("events") or []
    event: dict | None = None
    if events:
        event_id = events[0].get("id")
        try:
            async with httpx.AsyncClient(timeout=10) as ev_client:
                ev_resp = await ev_client.get(
                    f"{gamma_api_base_url.rstrip('/')}/events", params={"id": event_id}
                )
            ev_list = ev_resp.json() if ev_resp.status_code == 200 else []
        except httpx.HTTPError:
            ev_list = []
        if ev_list:
            event = ev_list[0]
    else:
        event_id = None

    category = _category_from_event(event)

    resolution_days_out = None
    end_date_iso = m.get("endDateIso")
    if end_date_iso:
        try:
            end_date = dt.datetime.fromisoformat(end_date_iso).replace(tzinfo=dt.timezone.utc)
            resolution_days_out = max((end_date - dt.datetime.now(dt.timezone.utc)).days, 0)
        except ValueError:
            resolution_days_out = None

    return MarketMeta(
        token_id=token_id,
        category=category,
        tick_size=Decimal(str(m.get("orderPriceMinTickSize", "0.01"))),
        min_order_size=Decimal(str(m.get("orderMinSize", "5"))),
        neg_risk=bool(m.get("negRisk", False)),
        tradeable=bool(m.get("acceptingOrders", False)) and not m.get("closed", False),
        event_id=str(event_id) if event_id else None,
        resolution_days_out=resolution_days_out,
        attrs=market_attrs(m, event),
    )


async def get_resolution_status(gamma_api_base_url: str, token_id: str) -> ResolutionStatus | None:
    """Whether `token_id`'s market has closed and, if so, whether this specific
    outcome won — for crediting *paper* positions on resolution (FR-EXE-10), not for
    live redemption (see ResolutionStatus's docstring for why those need different
    signals). Returns None on a lookup failure — callers must treat that as "don't
    know yet," never guess a result.

    A still-open market's `clob_token_ids` query returns nothing without
    `closed=true` (confirmed live, same asymmetry `queries._fetch_market_question`
    already works around) — so an *open* market and a *lookup failure* both come back
    empty here; that's fine, both mean "nothing to do yet" for this caller.

    `outcomePrices` and `clobTokenIds` are both JSON-encoded **strings**, not native
    arrays, despite how they print — confirmed by checking `type()` directly rather
    than trusting how they looked in a first pass (they read as bare lists at a
    glance). Each outcome's price resolves to exactly "1" (won) or "0" (lost) once
    the market is closed.
    """
    url = f"{gamma_api_base_url.rstrip('/')}/markets"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params={"clob_token_ids": token_id, "closed": "true"})
    if resp.status_code != 200:
        return None
    markets = resp.json()
    if not markets:
        return None
    m = markets[0]
    if not m.get("closed"):
        return ResolutionStatus(token_id=token_id, closed=False, won=None)

    try:
        clob_token_ids = json.loads(m["clobTokenIds"])
        outcome_prices = json.loads(m["outcomePrices"])
        idx = clob_token_ids.index(token_id)
        won = Decimal(outcome_prices[idx]) == 1
    except (KeyError, ValueError, TypeError, IndexError):
        return None

    return ResolutionStatus(token_id=token_id, closed=True, won=won)
