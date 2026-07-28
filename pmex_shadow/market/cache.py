"""Token metadata + book fetch. Phase 1 needs only the book fetch (for the paper
logger's VWAP simulation, FR-EXE-9); the full FR-M-1..4 metadata cache (tick size,
category, neg-risk flag, warmed + refreshed on a schedule) is Phase 2 scope, once
policy guards actually consume it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx

from pmex_shadow.models import BookSnapshot


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
