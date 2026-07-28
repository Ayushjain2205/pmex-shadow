# §9 Verification Log

All items below were checked against live endpoints, deployed contract source, and the
current official SDK source on **2026-07-29**, per PRD §9. Methods and raw evidence are
listed under each item so findings can be re-verified as the protocol evolves. Three
items materially **contradict** assumptions baked into the PRD/design doc — flagged with
⚠️ — and should be read before Phase 3/4 are implemented.

---

## 1. CTF Exchange V2 and NegRisk Exchange addresses on Polygon

**Verified.** Cross-checked across three independent sources and they agree:

| Contract | Address |
|---|---|
| CTF Exchange V2 (`standard_exchange`) | `0xE111180000d2663C0091e4f400237545B87B996B` |
| Neg Risk CTF Exchange V2 (`neg_risk_exchange`) | `0xe2222d279d744050d28e00520010520000310F59` |
| Conditional Tokens Framework (CTF) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| pUSD collateral token | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |

**Method:**
- `WebFetch https://docs.polymarket.com/resources/contracts` (2026-07-29)
- Fetched verified contract source/ABI directly from Polygonscan for both exchange
  addresses (`curl` on the address pages, confirmed `OrderFilled` event present in both)
- Cross-checked against `Polymarket/py-sdk` (official SDK, main branch, commit as of
  2026-07-28) — `src/polymarket/environments.py`, `PRODUCTION` env config, which hardcodes
  identical addresses.

⚠️ **Note:** `environments.py` also defines `exchange_v3 = 0xe3333700cA9d93003F00f0F71f8515005F6c00Aa`
and `protocol_v2_router = 0x12121212006e4CD160D18e3f00711DA5c3372600`. Live CLOB API
responses (`/markets`) and Data API trades observed during this pass all resolve against
the V2 addresses above, so V2 is confirmed as what's currently live — but re-check this
constant before Phase 3 in case V3 has since gone live.

---

## 2. `OrderFilled` event signature and indexed parameters

**Verified**, identical on both exchanges (fetched full ABI from Polygonscan verified
source for each address):

```solidity
event OrderFilled(
    bytes32 indexed orderHash,
    address indexed maker,
    address indexed taker,
    Side    side,               // NOT indexed (uint8)
    uint256 tokenId,            // NOT indexed
    uint256 makerAmountFilled,  // NOT indexed
    uint256 takerAmountFilled,  // NOT indexed
    uint256 fee,                // NOT indexed
    bytes32 builder,            // NOT indexed
    bytes32 metadata            // NOT indexed
);
```

`topic0 = 0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee`
(computed via `Web3.keccak(text="OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)")`
and confirmed by matching it against a real transaction's logs — see §3).

**Implication for `watcher/chain.py`:** only `orderHash`, `maker`, `taker` can be used in
a topic filter. **`tokenId` is not indexed** — you cannot topic-filter to specific
markets/tokens on-chain; every subscription is address-scoped only (matches FR-W-1/§3.1
as written, but rules out any future "filter by market" optimization at the log-filter
level — it would require decoding the non-indexed data first).

---

## 3. Maker vs. taker position for a copied target

**Verified empirically first, then confirmed against contract source — and the initial
empirical read was WRONG.** Recorded here in full because it's exactly the kind of
mistake "verify, don't guess" is supposed to catch, and it changes how `chain.py` is
built (one subscription, not two).

**First pass (live tx only, superseded below):** pulled a live Data API trade
(`proxyWallet=0x1230e394bdb4e28f67dc8b37996f1c28ec8edd03`, `side=BUY`,
`tx=0x8bacda89fe5d9108fa01cc568e91dd7239ea51b7848ea969995b7ce61eeb4e44`), decoded both
`OrderFilled` logs in that tx, and initially concluded the target appears as maker in one
log and taker in the other — so watching would need two topic filters (`topic2 ∈ targets`
and `topic3 ∈ targets`).

**That conclusion was wrong about *why*, which matters.** Fetched
`Polymarket/ctf-exchange-v2`'s `src/exchange/mixins/Trading.sol` and `Events.sol`
directly (2026-07-29) and checked every `_emitOrderFilledEvent` call site
(`_settleComplementaryMaker`, `_settleComplementaryTaker`, `_matchBuyOrders`,
`_distributeBuyMakerProceeds`, `_distributeSellMakerProceeds` — i.e. every match type:
COMPLEMENTARY, MINT, MERGE). Every single one follows the same pattern:

```solidity
_emitOrderFilledEvent(OrderFilledParams({
    maker: <the order owner's own address>,   // ALWAYS
    taker: <counterparty, or address(this) for the taker-order's own summary log>,
    side:  <that same owner's own order.side>,  // ALWAYS matches `maker`, never `taker`
    ...
}));
```

Concretely: when address X's order gets filled — whether X was the protocol-level
"taker" who submitted the matching order, or a "maker" whose resting order got hit — X
gets its own log where **X's address sits in the `maker` topic (topic2) and `side` is
X's own order side.** The `taker` topic (topic3) either holds the real counterparty
(on the resting maker's log) or `address(this)` — the exchange contract — on the
initiating order's own summary log. Re-examining the original tx confirms this exactly:
log 2 (where our target sat in the `maker` slot) was the target's *own* order summary;
log 1 (where the target sat in the `taker` slot) was the *counterparty's* own order
summary, not additional information about the target.

**Conclusion (corrected): a single topic filter suffices.**
`topic0 = OrderFilled sig`, `topic2 (maker) ∈ target_addresses` — every fill belonging to
a watched target produces a log matching this filter, with `side` directly usable,
regardless of whether that target acted as protocol maker or taker. **No second
`topic3`-based subscription is needed.** This simplifies FR-W-1/FR-W-2 versus what the
PRD assumed (§3.1 of the design doc calls for covering "both positions" — that's still
true in the sense that a target's trade is captured regardless of which role it played,
just not in the way originally assumed, i.e. via a second filter).

---

## 4. `py-clob-client` version, V2 compatibility, heartbeat/auto-cancel

⚠️ **Contradicts the PRD's assumed dependency.** `py-clob-client` is **archived and
unmaintained**:

- `GET https://api.github.com/repos/Polymarket/py-clob-client` → `"archived": true`,
  last push `2026-05-25` (README now says "no longer maintained," redirects to a new
  unified SDK).
- The official successor is **`Polymarket/py-sdk`** (active; last push `2026-07-28`, the
  day before this verification), published to PyPI as **`polymarket-client`**, latest
  version **`0.2.0`**, `requires-python >= 3.11` (compatible with the PRD's pinned
  Python 3.12).

**Action:** pin `polymarket-client==0.2.0` in place of `py-clob-client`. This changes
§3's "Exchange lib" row and affects `pyproject.toml` in Phase 0 and the `clob.py` wrapper
in Phase 3.

**Order types** — confirmed via `src/polymarket/models/clob/orders.py` in the SDK source:
```python
OrderType: TypeAlias = Literal["GTC", "GTD", "FAK", "FOK"]
MarketOrderType: TypeAlias = Literal["FAK", "FOK"]
```
GTD orders expire 1 minute before their stated expiration (safety buffer) and must be at
least 3 minutes in the future (per `docs.polymarket.com/trading/orders/overview`).

**Heartbeat / auto-cancel-on-disconnect** — the protocol-level feature **does exist**,
confirmed via `docs.polymarket.com/api-reference/trade/send-heartbeat`:
- `POST https://clob.polymarket.com/heartbeats`, authenticated with the standard L2
  headers (`POLY_API_KEY`, `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_PASSPHRASE`,
  `POLY_TIMESTAMP`); response `{"status": "ok"}`.
- If heartbeats stop, all open orders for that user are auto-cancelled. (The ~10s
  timeout + 5s buffer figure appears in secondary/aggregated sources, not stated on the
  primary docs page fetched above — treat as unconfirmed until observed empirically
  against a live account in Phase 3.)
- ⚠️ **The current official SDK (`polymarket-client` 0.2.0) does not wrap this
  endpoint** — grepped `clients/async_secure.py` and `_internal/actions/clob.py` in the
  SDK source, no heartbeat method exists. `execution/deadman.py` must call
  `POST /heartbeats` directly over HTTP with hand-built L2 auth headers; it cannot go
  through the SDK client. Per FR-EXE-7, use this native endpoint as the primary
  mechanism and keep the local watchdog as backup (there is also a known upstream
  complaint that heartbeat behavior has been unreliable — `Polymarket/rs-clob-client`
  issue #239, "Heartbeats feature for CLOB is fundamentally broken" — so the watchdog
  backup is not optional, treat it as load-bearing, not decorative).

---

## 5. Data API endpoint shape for user trades

**Verified.** `GET https://data-api.polymarket.com/trades`

- Response field is **`proxyWallet`** (lowercase hex string), confirmed via live sample.
- Filter query param is **`user`** (not `proxyWallet`) — confirmed working:
  `?user=0x1230e394bdb4e28f67dc8b37996f1c28ec8edd03&limit=2` returned only that address's
  trades, matching the `proxyWallet` field in the response.
- Tested with a **proxy wallet** address (as returned by the API itself). Did **not**
  isolate whether a raw EOA (pre-proxy) is also accepted — I don't have a verified
  EOA↔proxy pair to test the negative case cheaply. Since `doctor` already needs to
  resolve "proxy wallet per target" (FR-O-1) regardless, treat proxy-address resolution
  as mandatory before querying the Data API rather than relying on the API to accept an
  EOA — cheaper to verify once at target-onboarding time than to special-case it in the
  sweep hot path.
- `GET /positions?user=<proxy>` also confirmed working, and usefully exposes
  `redeemable` / `mergeable` booleans directly — worth using as a cheap pre-filter in
  `redeem.py` (FR-L-6) before doing the on-chain `payoutNumerators` check, not as a
  replacement for it.

---

## 6. Gamma metadata field names

**Verified**, two separate metadata surfaces exist with different naming conventions —
do not mix them carelessly in `market/cache.py`:

**Gamma API** (`https://gamma-api.polymarket.com`), camelCase:
- `/markets`: `conditionId`, `negRisk`, `orderPriceMinTickSize`, `orderMinSize`,
  `clobTokenIds`, `active` / `closed` / `archived` / `acceptingOrders`
- `/events`: `category` (string), `tags` (array of `{id, label, slug}`), `enableNegRisk`,
  `negRiskAugmented`

**CLOB API itself** (`https://clob.polymarket.com/markets`), snake_case:
- `minimum_order_size`, `minimum_tick_size`, `neg_risk`, `condition_id`,
  `tokens[].token_id`, `accepting_orders`, `tags`

Both are live and queryable without auth. Recommendation: use Gamma for
category/tags/event-level metadata (that's where `category`/`tags` actually live — the
CLOB market object has no category field), and either source for tick size / min order
size / neg-risk flag. Pick one as primary per field and document it in `cache.py` rather
than querying both per token.

---

## 7. CLOB order types and parameter names

**Verified** — see §4 above (`GTC`, `GTD`, `FAK`, `FOK`, confirmed from SDK source and
official docs).

---

## 8. CLOB rate limits and throttling response shape

**Verified** via direct fetch of `https://docs.polymarket.com/quickstart/introduction/rate-limits`:

| Endpoint | Burst | Sustained |
|---|---|---|
| `POST /order` | 5,000 / 10s | 120,000 / 10min |
| `DELETE /order` | 5,000 / 10s | 120,000 / 10min |
| `POST /orders` (batch) | 2,000 / 10s | 21,000 / 10min |
| `DELETE /orders` (batch) | 2,000 / 10s | 15,000 / 10min |
| `DELETE /cancel-all` | 250 / 10s | 6,000 / 10min |
| `/book`, `/price`, `/midpoint` | 1,500 / 10s | — |
| General CLOB (Cloudflare) | 9,000 / 10s | — |

- **Scope:** IP-based at the Cloudflare layer, **plus** a separate per-signer
  token-bucket limit specifically on order placement/cancellation endpoints. Both apply.
- **Throttling behavior:** docs state requests over the limit are "delayed/queued rather
  than immediately rejected" — but do **not** specify the HTTP status code, headers
  (e.g. `Retry-After`), or body shape for a throttled response. This could not be
  confirmed without a live authenticated account hitting the limit; `ratelimit.py`
  (FR-EXE-5) should be built defensively (treat any non-2xx or timeout as
  retry-with-backoff) and the exact throttle response shape should be captured
  empirically the first time it's hit in Phase 3, then recorded here.

---

## 9. Redemption entry points and on-chain resolution status

**Verified, with an unresolved discrepancy flagged.**

**Plain CTF path** — confirmed via Polygonscan verified ABI for
`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`:
```solidity
function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets) external;
function payoutNumerators(bytes32, uint256) external view returns (uint256);
function payoutDenominator(bytes32) external view returns (uint256);
```
Resolution status is read via `payoutDenominator(conditionId) != 0` (nonzero means the
condition has been reported by the oracle and payouts are set) — this is the correct
"is this actually redeemable on-chain" check called for in FR-L-6, distinct from a
market merely showing "resolved" in the UI.

**NegRisk path** — ⚠️ two official sources disagree:
- `docs.polymarket.com/resources/contracts` lists `NegRiskAdapter` at
  `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` and labels it **"CLOB v1, deprecated."**
- The current official SDK (`Polymarket/py-sdk`, main branch, 2026-07-28) sets
  `neg_risk_adapter = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"` — the *same* address —
  as its **active** config value, and its redemption call builders
  (`ctf_redeem_positions_call`, `redeem_v2_call` in
  `src/polymarket/_internal/actions/relayer/calls.py`) target it directly.
- There is also a `protocol_v2_router` (`0x12121212006e4CD160D18e3f00711DA5c3372600`)
  exposing a unified `redeem(bytes31,uint256,uint256)` selector
  (`redeem_v2_call`), suggesting redemption may be consolidating onto a router shared
  across plain and neg-risk markets.

**Do not silently pick one of these for Phase 4.** The safest read right now: use the
SDK's own call builders (`ctf_redeem_positions_call` for plain, `redeem_v2_call` via the
protocol v2 router for neg-risk/unified) rather than hand-rolling calldata against the
address docs.polymarket.com calls deprecated — the SDK is more likely to track
production reality than a docs page — but confirm against a real resolved neg-risk
market's redemption transaction before wiring `ledger/redeem.py`.

---

## 10. Relayer gas coverage for trades vs. redemption

⚠️ **Corrects the PRD/design doc's stated assumption.** §3.4 of the design doc states
redemption "is your own transaction" and costs POL, contrasted with gasless trades. This
is **only true for the direct on-chain path** — it is not the whole picture:

- `docs.polymarket.com/trading/gasless` (fetched directly) states the relayer covers,
  gaslessly: token approvals, token transfers, and **"split, merge, or redeem tokens"**
  — redemption is explicitly included in gasless coverage, not excluded.
- `github.com/Polymarket/agent-skills/blob/main/gasless.md` corroborates: relayer covers
  wallet deployment, approvals, **CTF split/merge/redeem**, and transfers.
- Access requires either **Relayer API Keys** (existing account, generated from
  Settings → API Keys) or **Builder API credentials** (Builder Program membership),
  tied to a "Deposit Wallet." A plain EOA wallet without one of these does **not** get
  gasless redemption and pays its own gas.
- Direct on-chain `redeemPositions` cost is reported around **~0.02 POL per call** by
  third-party sources (not Polymarket's own docs — treat as a rough estimate, not a
  guaranteed figure, and re-measure against current Polygon gas prices before sizing the
  POL float in `doctor`).

**Action:** `ledger/redeem.py` should attempt the gasless relayer path first (if the
bot's credentials support it) and fall back to a direct on-chain transaction — keeping
FR-L-10's POL-balance-check-before-attempting as the safety net for the fallback path,
not as the assumed primary path.

---

## 11. `eth_getLogs` block-range cap

**Confirmed as provider-dependent — no universal value, matches the design doc's
existing caveat.** No single number to record; ranges observed across sources: as low as
50–1,000 blocks on some public/free endpoints, QuickNode explicitly documents a
10,000-block cap, others cap by **result count** (e.g. 10,000 matched logs) rather than
block range regardless of the window requested.

**Action:** keep `backfill_chunk_blocks` (default 2000, per FR-W-4) as a conservative
configurable default — it is safely under every observed cap. `sweep.py`/`chain.py`
should treat an oversized-range error as a signal to shrink and retry (halve the window)
rather than trusting a hardcoded constant, since the real cap depends on whichever RPC
provider the operator configures.

---

## Summary of deviations from the PRD as written

1. **§3 "Exchange lib"** should read `polymarket-client` (PyPI), from `Polymarket/py-sdk`,
   not `py-clob-client` — the latter is archived. Pin `polymarket-client==0.2.0`.
2. **FR-EXE-7**: the native heartbeat/auto-cancel endpoint exists (`POST /heartbeats`)
   but isn't wrapped by the new SDK — must be called directly, and its reliability is
   disputed upstream, so the local watchdog is not optional backup, it's required.
3. **§3.4 / FR-L-10**: redemption can be gasless via the relayer (contradicts "redemption
   is your own transaction" as an absolute statement) — but only for bots with
   Relayer/Builder API credentials configured; POL-float checks stay as the fallback-path
   safety net, not the primary assumption.
4. **NegRiskAdapter address** has conflicting "current" vs. "deprecated" labeling
   between docs.polymarket.com and the live SDK config — unresolved, must be reconfirmed
   against a real neg-risk redemption tx before Phase 4 ships.

---

## Addendum: findings from building Phase 1 (2026-07-29)

Two more things surfaced while implementing and live-testing `watcher/chain.py` against
a real Polygon WSS endpoint and real target wallets — recorded here because both are
exactly the "verify, don't guess" failure mode the PRD warns about, and both would have
been easy to ship wrong.

**12. An empty list at an `eth_subscribe`/`eth_getLogs` topic position matches
*everything*, not nothing.** Tested directly against a live provider
(`wss://polygon-bor-rpc.publicnode.com`): subscribing with
`topics: [ORDER_FILLED_TOPIC0, null, []]` (i.e. maker-topic filter with zero target
addresses) delivered *every* `OrderFilled` log on the exchange, unfiltered — confirmed
by receiving a log within 15s whose maker address wasn't in the (empty) target set.
This is intuitive in hindsight (each topic *position* is a disjunction over its own
list; an empty disjunction is vacuously unsatisfied... except providers don't implement
it that way) but it's not something to assume. `chain.py` now explicitly refuses to
subscribe when the target set is empty — it idles and polls `target_stats` every 10s
instead. Getting this wrong would have meant a watcher with zero configured targets
silently ingesting and storing the entire exchange's fill stream.

**13. Free-tier public RPC (publicnode, and presumably similar free providers) rejects
`eth_getLogs` outside a small recent-block window** with
`{"code":-32602,"message":"Archive requests require a personal token..."}` — a 403,
not a graceful empty result. This is provider-side confirmation of the design doc's own
warning (§3.1: "Public RPCs mostly won't do") — it's not just about missed
subscriptions, `eth_getLogs` backfill is also restricted. `backfill()`'s
shrink-and-retry logic (for the *block-range-too-large* case, docs/VERIFIED.md item 11)
correctly does **not** treat this as recoverable by shrinking further — it exhausts
retries and surfaces as a reconnect, which is the right behavior; a real deployment
needs a paid RPC tier for any backfill deeper than a few thousand blocks.

Both findings confirmed empirically end-to-end in Docker: a live target
(`0x1230e394bdb4e28f67dc8b37996f1c28ec8edd03`) produced correctly-decoded,
correctly-sided `target_fills` rows from **both** the chain subscription (source=chain,
real block numbers) and the Data API sweep (source=dataapi) independently, with no
duplicate rows — see the Phase 1 commit for the captured evidence.
