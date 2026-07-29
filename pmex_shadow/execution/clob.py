"""Thin `polymarket-client` wrapper (docs/VERIFIED.md item 4). Two-step build/submit
matches FR-EXE-3 exactly: `create_limit_order()` builds+signs locally with no network
call (the "built" row), `post_order()` submits it (the "submitted" transition).

Also wraps the native heartbeat/auto-cancel endpoint (`POST /heartbeats`) directly
over HTTP with hand-built L2 auth headers — confirmed in VERIFIED.md that the SDK
does not expose this endpoint itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import httpx
from polymarket import PRODUCTION, AsyncSecureClient
from polymarket._internal.hmac import build_hmac_signature
from polymarket.models.clob.api_key import ApiKeyCreds
from polymarket.models.clob.order_response import AcceptedOrder, RejectedOrder
from polymarket.models.clob.orders import SignedOrder

from pmex_shadow.models import Side


@dataclass
class OrderResult:
    accepted: bool
    exchange_order_id: str | None
    status: str | None  # SDK's OrderPostStatus: 'live' | 'matched' | 'delayed'
    making_amount: Decimal | None
    taking_amount: Decimal | None
    error: str | None


class ClobClient:
    """One instance per bot process — holds the authenticated SDK client."""

    def __init__(self, client: AsyncSecureClient) -> None:
        self._client = client

    @classmethod
    async def create(cls, *, private_key: str, wallet: str, api_key: str, api_secret: str, api_passphrase: str) -> "ClobClient":
        credentials = ApiKeyCreds(apiKey=api_key, secret=api_secret, passphrase=api_passphrase)
        client = await AsyncSecureClient.create(
            private_key=private_key, wallet=wallet, environment=PRODUCTION, credentials=credentials,
        )
        return cls(client)

    async def close(self) -> None:
        await self._client.close()

    async def build_limit_order(self, *, token_id: str, price: Decimal, size: Decimal, side: Side) -> SignedOrder:
        """Build and sign locally — no network call to place it. This IS the "built"
        state (FR-EXE-3): the caller persists the order row as `built` right after
        this returns, before calling `submit_signed_order`.
        """
        return await self._client.create_limit_order(token_id=token_id, price=price, size=size, side=side.value)

    async def submit_signed_order(self, signed: SignedOrder) -> OrderResult:
        """Submit an already-built order. Separate from build_limit_order() so the
        caller controls exactly when the `built` -> `submitted` transition (and its
        timeout clock, FR-EXE-4) starts.
        """
        result = await self._client.post_order(signed)
        if isinstance(result, AcceptedOrder):
            return OrderResult(
                accepted=True, exchange_order_id=result.order_id, status=result.status,
                making_amount=result.making_amount, taking_amount=result.taking_amount, error=None,
            )
        assert isinstance(result, RejectedOrder)
        return OrderResult(accepted=False, exchange_order_id=None, status=None, making_amount=None, taking_amount=None, error=f"{result.code}: {result.message}")

    async def cancel_order(self, exchange_order_id: str) -> bool:
        result = await self._client.cancel_order(order_id=exchange_order_id)
        return bool(result)

    async def cancel_all(self) -> bool:
        result = await self._client.cancel_all()
        return bool(result)

    async def get_order_status(self, exchange_order_id: str) -> dict | None:
        """Query-based reconciliation for the `unknown` state (FR-EXE-4) — never
        blind-retry a timed-out submission; ask the exchange what actually happened."""
        try:
            order = await self._client.get_order(order_id=exchange_order_id)
        except Exception:
            return None  # not found: either never existed, or fully filled and no longer "open"
        return {
            "status": order.status,
            "size_matched": order.size_matched,
            "original_size": order.original_size,
        }


def build_heartbeat_headers(*, api_key: str, secret: str, passphrase: str, address: str) -> dict[str, str]:
    """POST /heartbeats needs the standard L2 auth headers; the SDK doesn't wrap this
    endpoint itself (VERIFIED.md item 4), but it does export the exact HMAC signing
    routine it uses internally for every other authenticated call
    (`polymarket._internal.hmac.build_hmac_signature`) — reused here rather than
    reimplemented, so this can't silently drift from what the SDK actually does.
    """
    timestamp = int(time.time())
    signature = build_hmac_signature(secret=secret, timestamp=timestamp, method="POST", path="/heartbeats")
    return {
        "POLY_ADDRESS": address,
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": passphrase,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(timestamp),
    }


async def send_heartbeat(clob_base_url: str, *, api_key: str, secret: str, passphrase: str, address: str) -> bool:
    headers = build_heartbeat_headers(api_key=api_key, secret=secret, passphrase=passphrase, address=address)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{clob_base_url.rstrip('/')}/heartbeats", headers=headers)
    return resp.status_code == 200
