import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from pmex_shadow.models import BookSnapshot, Side
from pmex_shadow.watcher.normalize import (
    decode_order_filled_log,
    target_fill_from_chain_log,
    target_fill_from_dataapi_trade,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"

# real_order_filled_logs.json: the three OrderFilled logs from a real Polygon tx
# (0xc88f7c1e2cb2a7847a63f28ef57c11c51543b32ed8104d13a0b69540ebd69098), fetched via
# eth_getTransactionReceipt and captured while building docs/VERIFIED.md item 3's
# correction. log[2] is the target's own order summary (maker == target).
TARGET = "0x1aecd5dd634beca71db3933660d68426c0403bdc"


def _load_logs():
    return json.loads((FIXTURES / "real_order_filled_logs.json").read_text())


def test_decode_real_log_matches_known_values():
    logs = _load_logs()
    decoded = decode_order_filled_log(logs[2])
    assert decoded["maker"] == TARGET
    assert decoded["taker"] == "0xe111180000d2663c0091e4f400237545b87b996b"  # exchange itself
    assert decoded["side"] == 0  # BUY per Structs.sol
    assert decoded["token_id"] == 60398623454518672215750307511107004728586134051350133978272613659550941782240
    assert decoded["maker_amount_filled"] == 999999
    assert decoded["taker_amount_filled"] == 1492536
    assert decoded["fee"] == 23090


def test_target_fill_only_built_when_target_is_maker():
    logs = _load_logs()
    detected_at = dt.datetime(2026, 7, 29, tzinfo=dt.timezone.utc)
    block_ts = dt.datetime(2026, 7, 29, 0, 0, tzinfo=dt.timezone.utc)

    # log[0] and log[1]: target is in the `taker` topic, not `maker` — must be skipped,
    # per the corrected finding in docs/VERIFIED.md item 3 (that log belongs to the
    # counterparty, not the target).
    for log in logs[:2]:
        decoded = decode_order_filled_log(log)
        assert target_fill_from_chain_log(decoded, TARGET, block_ts, detected_at) is None

    # log[2]: target is the maker of this log — this is the one that becomes a fill.
    decoded = decode_order_filled_log(logs[2])
    fill = target_fill_from_chain_log(decoded, TARGET, block_ts, detected_at)
    assert fill is not None
    assert fill.side == Side.BUY
    assert fill.target == TARGET
    assert fill.size == Decimal("1.492536")
    assert fill.notional_usd == Decimal("0.999999")
    assert fill.price == fill.notional_usd / fill.size
    assert fill.source == "chain"
    assert fill.dedupe_key == f"{decoded['tx_hash']}:{decoded['log_index']}"


def test_dataapi_trade_normalizes_consistently_with_chain_decode():
    # Values here are derived from the same real on-chain event as the fixture above
    # (not invented) — the live Data API record itself had already aged out of the
    # endpoint's rolling window by the time this fixture was captured, so this
    # reconstructs the equivalent record from verified on-chain amounts.
    trade = json.loads((FIXTURES / "real_dataapi_trade.json").read_text())
    detected_at = dt.datetime(2026, 7, 29, tzinfo=dt.timezone.utc)
    fill = target_fill_from_dataapi_trade(trade, detected_at)

    assert fill.target == TARGET
    assert fill.side == Side.BUY
    assert fill.source == "dataapi"
    assert fill.price == Decimal("0.67")
    assert fill.size == Decimal("1.492536")
    assert fill.notional_usd == Decimal("0.67") * Decimal("1.492536")


def test_vwap_for_walks_the_book_and_handles_thin_liquidity():
    book = BookSnapshot(
        token_id="t",
        bids=[(Decimal("0.50"), Decimal("100")), (Decimal("0.49"), Decimal("50"))],
        asks=[(Decimal("0.51"), Decimal("100")), (Decimal("0.52"), Decimal("50"))],
        taken_at=dt.datetime.now(dt.timezone.utc),
    )

    # BUY walks the asks: $51 buys exactly the first level (100 shares @ 0.51).
    price, shares = book.vwap_for(Side.BUY, Decimal("51"))
    assert price == Decimal("0.51")
    assert shares == Decimal("100")

    # BUY that exhausts the first ask level spills into the second.
    price, shares = book.vwap_for(Side.BUY, Decimal("61.5"))
    expected_shares = Decimal("100") + Decimal("10.5") / Decimal("0.52")
    assert shares == expected_shares
    assert price == Decimal("61.5") / shares

    # SELL walks the bids.
    price, shares = book.vwap_for(Side.SELL, Decimal("25"))
    assert price == Decimal("0.50")
    assert shares == Decimal("50")

    # More notional than the book can absorb: fills what's there, no more.
    total_book_usd = Decimal("0.50") * 100 + Decimal("0.49") * 50
    price, shares = book.vwap_for(Side.SELL, total_book_usd + Decimal("1000"))
    assert shares == Decimal("150")


def test_vwap_for_empty_book_returns_zero():
    book = BookSnapshot(token_id="t", bids=[], asks=[], taken_at=dt.datetime.now(dt.timezone.utc))
    price, shares = book.vwap_for(Side.BUY, Decimal("10"))
    assert price == Decimal(0)
    assert shares == Decimal(0)
