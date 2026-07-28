"""Verified on-chain constants (docs/VERIFIED.md, checked 2026-07-29). One source of
truth — doctor.py, watcher/chain.py, and watcher/normalize.py all import from here
rather than re-hardcoding addresses that could drift out of sync.
"""

from __future__ import annotations

CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
PUSD_COLLATERAL = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

ORDER_FILLED_SIGNATURE = (
    "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)"
)
# keccak256(ORDER_FILLED_SIGNATURE) — verified against Polygonscan's ABI for both
# exchanges above and confirmed by matching real tx logs (docs/VERIFIED.md item 2).
ORDER_FILLED_TOPIC0 = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"

# Non-indexed field order in OrderFilled's `data` blob (see docs/VERIFIED.md item 2).
ORDER_FILLED_DATA_TYPES = ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"]

# Side enum from ctf-exchange-v2 src/exchange/libraries/Structs.sol: BUY=0, SELL=1.
SIDE_BUY = 0
SIDE_SELL = 1
