"""Normalize chain logs and Data API trades into identical `TargetFill` values
(FR-W-10). Pure functions — no I/O, no clock reads (aside from `detected_at`, which is
intentionally "now," the whole point being to measure detection latency).

Side derivation for the chain path (docs/VERIFIED.md item 3, corrected finding): in
every `OrderFilled` log, the address in the `maker` topic (topic2) is always the order
owner, and `side` is always *that owner's own* order side — regardless of whether they
were the protocol-level maker or taker. So: only consume logs where `maker == target`,
and use `side` directly. Logs where a target appears only in the `taker` topic (topic3)
are the *counterparty's* own summary log, not additional data about the target — skip
them, per the watcher's single maker-topic-filter subscription.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from eth_abi import decode as abi_decode

from pmex_shadow.contracts import ORDER_FILLED_DATA_TYPES, SIDE_BUY
from pmex_shadow.models import Side, TargetFill


def decode_order_filled_log(log: dict[str, Any]) -> dict[str, Any]:
    """Decode one raw eth_getLogs/eth_subscribe OrderFilled log into its fields.

    Does not filter or interpret maker/taker — that's the caller's job, since it
    depends on which address you're watching for.
    """
    topics = log["topics"]
    order_hash = topics[1]
    maker = "0x" + topics[2][-40:]
    taker = "0x" + topics[3][-40:]
    raw = bytes.fromhex(log["data"][2:] if log["data"].startswith("0x") else log["data"])
    side, token_id, maker_amount_filled, taker_amount_filled, fee, builder, metadata = abi_decode(
        ORDER_FILLED_DATA_TYPES, raw
    )
    return {
        "order_hash": order_hash,
        "maker": maker.lower(),
        "taker": taker.lower(),
        "side": side,
        "token_id": token_id,
        "maker_amount_filled": maker_amount_filled,
        "taker_amount_filled": taker_amount_filled,
        "fee": fee,
        "tx_hash": log["transactionHash"],
        "log_index": int(log["logIndex"], 16) if isinstance(log["logIndex"], str) else log["logIndex"],
        "block_number": int(log["blockNumber"], 16) if isinstance(log["blockNumber"], str) else log["blockNumber"],
    }


# Polymarket prices/sizes: price is in the exchange's native tick units (6 decimals per
# PRD §5 NUMERIC(18,6)); on-chain amounts are integer base units. makerAmountFilled and
# takerAmountFilled are both in base units (collateral has 6 decimals, outcome tokens
# have 6 decimals) — price = collateral_amount / share_amount, sized correctly, without
# assuming which side (maker/taker amount) is collateral vs shares: that depends on the
# owner's own side, which we already have.
_BASE_UNIT_SCALE = Decimal(10) ** 6
_DEDUPE_QUANTIZE = Decimal("0.000001")  # matches target_fills.price/size's NUMERIC(_,6) scale


def _dedupe_key(target: str, tx_hash: str, token_id: str, side: Side, price: Decimal, size: Decimal) -> str:
    """Cross-source trade identity (FR-W-6): chain and dataapi see the same real fill
    through different payloads with no shared precise event id — the Data API's
    `/trades` response has no log-index/order-hash equivalent (confirmed against the
    raw payload, see docs/VERIFIED.md) — so identity is target+tx+token+side+price+size,
    quantized to the column's own NUMERIC(_,6) scale so chain's raw division and
    dataapi's already-rounded display price collapse to the same string for the same
    trade. Only blind spot: the same wallet filling the same outcome token, same side,
    at the exact same price *and* size, twice in one tx (matched against two different
    counterparties) — collapses onto one row. Rare, and no worse than what the dataapi
    path alone could ever tell apart anyway."""
    return (
        f"{target.lower()}:{tx_hash}:{token_id}:{side.value}:"
        f"{price.quantize(_DEDUPE_QUANTIZE)}:{size.quantize(_DEDUPE_QUANTIZE)}"
    )


def target_fill_from_chain_log(
    decoded: dict[str, Any],
    target: str,
    block_ts: dt.datetime,
    detected_at: dt.datetime,
) -> TargetFill | None:
    """Build a TargetFill from a decoded log, for one specific target address.

    Returns None if `target` isn't the order owner in this log (i.e. it's only in the
    `taker` topic) — see module docstring for why that log is skipped, not flipped.
    """
    if decoded["maker"] != target.lower():
        return None

    side = Side.BUY if decoded["side"] == SIDE_BUY else Side.SELL
    # For the order owner's own log: makerAmountFilled is what THEY gave up,
    # takerAmountFilled is what THEY received. A BUY gives up collateral (pUSD) for
    # shares; a SELL gives up shares for collateral.
    if side == Side.BUY:
        collateral_amount, share_amount = decoded["maker_amount_filled"], decoded["taker_amount_filled"]
    else:
        share_amount, collateral_amount = decoded["maker_amount_filled"], decoded["taker_amount_filled"]

    shares = Decimal(share_amount) / _BASE_UNIT_SCALE
    notional_usd = Decimal(collateral_amount) / _BASE_UNIT_SCALE
    price = (notional_usd / shares) if shares > 0 else Decimal(0)
    token_id = str(decoded["token_id"])

    return TargetFill(
        dedupe_key=_dedupe_key(target, decoded["tx_hash"], token_id, side, price, shares),
        target=target.lower(),
        token_id=token_id,
        side=side,
        price=price,
        size=shares,
        notional_usd=notional_usd,
        block_number=decoded["block_number"],
        block_ts=block_ts,
        detected_at=detected_at,
        source="chain",
    )


def target_fill_from_dataapi_trade(trade: dict[str, Any], detected_at: dt.datetime) -> TargetFill:
    """Build a TargetFill from one Data API `/trades` record.

    Verified field names (docs/VERIFIED.md item 5): `proxyWallet`, `side`, `asset`
    (token id), `price`, `size`, `timestamp` (unix seconds), `transactionHash`.
    """
    price = Decimal(str(trade["price"]))
    size = Decimal(str(trade["size"]))
    side = Side.BUY if trade["side"] == "BUY" else Side.SELL
    token_id = str(trade["asset"])
    target = trade["proxyWallet"].lower()
    block_ts = dt.datetime.fromtimestamp(int(trade["timestamp"]), tz=dt.timezone.utc)
    return TargetFill(
        dedupe_key=_dedupe_key(target, trade["transactionHash"], token_id, side, price, size),
        target=target,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        notional_usd=price * size,
        block_number=None,
        block_ts=block_ts,
        detected_at=detected_at,
        source="dataapi",
    )
