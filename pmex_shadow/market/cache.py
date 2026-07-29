"""Token metadata + book fetch (FR-M-1..4). The book fetch shipped in Phase 1 (needed
by the paper logger); market metadata ships now that Phase 2's policy selectors
actually consume it. No persistent warm/refresh cache yet (FR-M-2) — that's still
Phase 2 scope but deferred past this first cut; every call here hits Gamma live.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx

from pmex_shadow.models import BookSnapshot, MarketMeta


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

    category = None
    events = m.get("events") or []
    if events:
        event_id = events[0].get("id")
        try:
            ev_resp = await httpx.AsyncClient(timeout=10).get(
                f"{gamma_api_base_url.rstrip('/')}/events", params={"id": event_id}
            )
            ev_list = ev_resp.json() if ev_resp.status_code == 200 else []
        except httpx.HTTPError:
            ev_list = []
        if ev_list:
            tags = ev_list[0].get("tags") or []
            non_generic = [t["label"] for t in tags if t.get("label") and t["label"].lower() != "all"]
            category = non_generic[0] if non_generic else None
    else:
        event_id = None

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
    )
