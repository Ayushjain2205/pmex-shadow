# pmex-shadow

Copy-trading execution infrastructure for Polymarket. Clone, configure, `docker compose up`.

> Ships in **paper mode**. Going live is an explicit, deliberate action.

---

## 1. Design principles

Everything below follows from these five.

**Event-sourced.** The target's fill log is the only source of truth. Intents, orders, positions and PnL are derived. This is what makes it possible to replay six weeks of activity against changed policy without touching live code — the difference between tuning guards empirically and guessing.

**Shared watcher, isolated bots.** One watcher for the deployment; N bot processes, each with its own wallet, policy, ledger and capital. Within a bot: one asyncio process, one router, one queue, per-market ordering. Fan-out is Postgres `LISTEN/NOTIFY` — no Kafka, no Redis.

**Isolation beats netting.** A bot owns its wallet outright. Costs capital fragmentation; buys unambiguous reconciliation, contained blast radius, and per-bot PnL with no attribution guesswork.

**Mode is first-class.** `watch` → `paper` → `live` run the *same code path*, differing only at the final submit. A bug you hit in paper is a bug you'd have hit live.

**Latency is not the product.** End-to-end is ~1.5–2.5s, dominated by Polygon block time and network RTT. Correctness, guards and capital discipline are where the returns are — not shaving milliseconds off a two-second budget.

---

## 2. Topology

The decision everything hangs off: **do bots share a wallet?** They do not.

If they did, they wouldn't be independent bots — they'd be strategies in one process, and you'd inherit every problem isolation exists to solve. The reconciler couldn't attribute a position. A sports bot buying YES against a whale bot buying NO leaves you holding both sides: two spreads paid, zero exposure. Collateral goes to whoever fires first.

```
   Polygon WS ──┐
                ├──▶  pmex-shadow watcher     (1×, shared)
   Data API  ───┘             │
                              ▼
                    fills table + LISTEN/NOTIFY
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  bot: sports           bot: whales           bot: politics
  wallet A              wallet B              wallet C
```

The watcher is shared because the log stream is *identical* regardless of who consumes it. Running it per-bot multiplies the RPC bill and normalizes N times for zero benefit.

### 2.1 Bot definition

A bot is five things: a name, a wallet, a target list, a selector set, and a policy. One YAML per bot in `bots/`; adding a bot is dropping a file.

**The name is an opaque identifier, not a category.** `bot1`, `sports_bot1`, `whale_tight`, `experiment_3` are all equally valid. Nothing in the system infers behaviour from the string — scope comes entirely from `selectors` and `targets`.

```yaml
# bots/sports_bot1.yaml
name: sports_bot1
wallet: { funder_env: SPORTS_BOT1_FUNDER, pk_env: SPORTS_BOT1_PK }
selectors:
  categories: [sports]
  min_book_liquidity_usd: 500
targets: [whale1, whale2, sharp3]
policy: { profile: tight }
risk: { envelope_usd: 1500 }
```

```yaml
# bots/bot2.yaml
name: bot2
wallet: { funder_env: BOT2_FUNDER, pk_env: BOT2_PK }
selectors:
  min_target_notional_usd: 5000        # size only — no category filter
  max_time_to_resolution_days: 90
targets: [whale1, whale4]
policy: { profile: loose }
risk: { envelope_usd: 3000 }
```

Selectors are optional filters that compose — category, target notional floor, book liquidity, time to resolution, event IDs. A fill is copied only if every selector passes. **A bot with no selectors copies every fill from its targets.**

Policy profiles are named for what they *do*, not for a market type: `tight`, `loose`, `gapping_markets`. Naming them `live_sports` bakes the category assumption back into config.

### 2.2 Overlap between bots

Two bots may deliberately share targets *and* selectors — running `sports_bot1` and `sports_bot2` on identical flow with different guard tightness is a legitimate way to A/B a policy. Three consequences:

**Overlap is silent double exposure.** If both bots accept the same fill you copy it twice, in two wallets, with no netting to catch it — netting operates only within a bot. Intentional if you meant it, invisible if you didn't. Provide `pmex-shadow bots overlap` (target ∩ selector intersections) and warn at creation: *"bot2 overlaps sports_bot1 on 3 targets with identical selectors; combined exposure will be 2×."*

**`global_max_exposure_usd` needs an owner.** No individual bot can enforce it — each knows only its own wallet. It must be a read against the shared positions table before firing. Architecturally fine, since Postgres is already the join point, but it has to exist deliberately rather than being assumed.

**Names are immutable.** The name is the `bot_id` in the idempotency key, the position ledger, the docker service, the env file path and the metrics label. Renaming orphans the ledger. Forbid it, or make it an explicit `migrate` command — never let someone edit `name:` in a YAML and silently fork their own history.

### 2.2 Wallet provisioning and treasury

Each bot needs its own EOA → proxy wallet → pUSD balance, approvals and L2 credentials.

`pmex-shadow bot new <name>` does all of it: accept or generate a key, derive the proxy address, derive L2 creds from an EOA signature, print the funding address, check approvals. `doctor --bot <name>` re-verifies on every deploy.

**Key management is your largest security surface.** N private keys on one VPS, and isolation collapses if they leak together.

- Keys in `secrets/<bot>.env`, mode `0600`, one file per bot. Never in `bots/*.yaml` — those hold env var *names*, and they're the files you'll be tempted to commit.
- Non-root container user. No keys in any image layer.
- At meaningful size, a remote signer (KMS) rather than raw keys on disk.

**Rebalancing is an on-chain transfer, not a config edit.** Keep a separate funding wallet and do it deliberately:

```
pmex-shadow treasury fund sports --usd 500
pmex-shadow treasury sweep politics --to funding
```

**Bots never move funds between themselves automatically.** A rebalancing bug draining the wrong wallet is precisely what isolation exists to prevent. Treasury operations are operator-initiated, always, and CLI-only — never in the web UI.

**Gas.** Trades route through Polymarket's relayer and cost nothing, but approvals, redemptions and merges are your own transactions. Every bot wallet needs a small POL float, and `doctor` must fail loudly when one runs dry — otherwise the first symptom is a redemption silently failing on a market that resolved months ago.

---

## 3. Components

### 3.1 `watcher/`

| Module | Job |
|---|---|
| `chain.py` | WS subscription on CTF Exchange **V2** + NegRisk exchange, `OrderFilled` topic-filtered to target addresses. Auto-reconnect with gap detection. |
| `sweep.py` | Data API poll every 30s per target. Backfills what the socket missed during reconnects. Never the primary path. |
| `normalize.py` | Decodes both sources into one `TargetFill`. |
| `heartbeat.py` | Writes a liveness row every 5s. Bots halt themselves when it goes stale. |

**Verify before writing the filter:** which `OrderFilled` fields are indexed in the V2 ABI, and which field your targets actually appear in. Pull known historical fills and confirm empirically rather than reasoning from contract source.

```python
@dataclass(frozen=True)
class TargetFill:
    dedupe_key: str        # f"{tx_hash}:{log_index}" or data-api trade id
    target: str            # proxy wallet
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    block_ts: datetime
    detected_at: datetime  # latency telemetry
    source: Literal["chain", "dataapi"]
```

**The watcher is a single point of failure.** Every bot checks the heartbeat and halts on staleness. Silence must mean "I am blind," never "nothing is happening" — a bot trading on a stale stream is far worse than one that stopped.

#### RPC requirements

The one external dependency with a bill attached.

- **WSS endpoint** with reliable `eth_subscribe`. Public RPCs mostly won't do — they either don't support persistent subscriptions or drop them silently under load, and you won't notice until you've missed a day of fills. Use a provider (Alchemy, QuickNode, Chainstack, dRPC). Free tier is fine at 5 bots; ~$50/mo beyond that.
- **Full node is enough** — no archive needed. You read recent logs plus a bounded backfill window.
- **Configure a fallback provider.** WS connections drop and provider outages are routine; primary + secondary with auto-failover is an hour of work.

**Persist `last_processed_block`.** On restart the watcher resumes from there, never from head — otherwise every deploy leaves a hole. Gap recovery is a chunked `eth_getLogs` loop: providers cap block ranges (commonly 2k–10k) and Polygon at ~2s blocks is ~43k blocks/day, so recovering a day-long outage is paginated, not a single call.

**The chain source is optional.** For politics and pre-game sports, Data-API-only is genuinely viable — 2s versus 8s costs nothing in those markets. Keep the chain path pluggable so users without an RPC budget can run degraded rather than not at all:

```yaml
watcher:
  sources:
    chain:
      enabled: true
      ws_url_env: POLYGON_WS_URL
      ws_fallback_url_env: POLYGON_WS_URL_FALLBACK
      backfill_chunk_blocks: 2000
    dataapi:
      enabled: true
      poll_interval_s: 30
```

Note that the argument for the chain path is **completeness and scale, not latency**. Logs are ground truth where the Data API is an indexer that can lag or drop; and one subscription covers all targets, whereas polling is N requests × interval — at 200 targets on a 5s poll you are at 40 req/s sustained and rate-limited. That ceiling is the real reason to keep it.

### 3.2 `market/`

Token metadata cache: tick size, min order size, neg-risk flag, category, event ID, resolution date. Warmed at discovery, refreshed on a slow loop.

**Category resolution is hot-path** because scoped bots filter on it. For an unknown token a scoped bot must **fail closed** — skip, fetch async, catch it next time. Never guess a category to avoid missing a trade.

**Neg-risk markets need explicit handling.** Multi-outcome events route through the NegRisk adapter with convert operations. A target's neg-risk fill copied as if it were a plain binary can leave you in a position you cannot exit the way they will. Either implement convert support or have the selector exclude neg-risk markets — but decide, don't discover.

### 3.3 `policy/`

Pure functions. `TargetFill + BookSnapshot + LedgerState → Intent | Skip(reason)`. No I/O, fully unit-testable, backtestable against stored fills.

- `guards.py` — slippage vs. target price; volatility (book moved >N ticks in 5s → gapping, skip); market-type profile; staleness; resolution proximity.
- `sizing.py` — see below. Sizing at small capital is mostly a *selection* problem, not a scaling one.
- `netting.py` — collapses opposing and duplicate intents *within* a bot. Two targets in your sports bot can still take opposite sides of the same game; that's still two spreads for zero exposure.

Every `Skip` is persisted with its reason. Skip-rate by reason is a primary health metric — a sudden spike usually means a market classification bug, not a market regime change.

#### Sizing model

Ratio-scaling fails on a small wallet: a target trading $10,000 against your $500 envelope gives you a $0.50 position, below the exchange minimum and meaningless anyway. Bankroll-proportional sizing is theoretically right but you can only estimate their bankroll — on-chain balance misses funds held elsewhere and across other wallets, and it drifts constantly.

Normalize against **their own size distribution** instead. You don't need their bankroll; you need to know whether this trade is big *for them*. That's computable purely from observed fills.

```yaml
sizing:
  mode: target_size_percentile
  base_unit_usd: 25                # your size for a median trade of theirs
  curve: [{p: 50, mult: 1.0}, {p: 80, mult: 1.5}, {p: 95, mult: 2.5}]
  min_target_size_percentile: 60   # skip their below-median trades entirely
  min_order_usd: 5
  max_position_usd: 75
  max_concurrent_positions: 8
  reserve_pct: 20
```

Worked example — target's distribution is p50 $2k, p80 $6k, p95 $15k; a $10,000 buy at 0.62 arrives:

1. $10k interpolates to **p88** of her distribution
2. Curve at p88 → **2.0×**
3. `$25 × 2.0` = **$50**
4. Under `max_position_usd: 75` → passes
5. Envelope $500 less 20% reserve = $400 deployable; $310 held across 6 positions → $90 free, 6 < 8 → passes
6. Guards: she filled 0.62, best ask 0.63, tolerance 2 ticks → passes
7. `$50 / 0.63` = 79.36 → **round down to 79 shares** ($49.77), above `min_order_usd`
8. Submit limit buy, 79 shares @ 0.64

Her $10,000 became your $50 — not by dividing by 200, but because it was a high-conviction trade for her and $50 is what conviction looks like on a $500 wallet.

The same target buying $1,200 sits at ~p35, below `min_target_size_percentile`, and is skipped as `below_target_percentile` before sizing is even reached. **This is the line that does the real work on a small wallet.** A target placing 20 fills a week, copied at $10 each, leaves you fully deployed by Thursday holding their low-conviction noise. Copy the 6 they leaned into instead.

**Capital velocity is the binding constraint, not position size.** Politics markets lock capital for weeks; a $500 envelope supports maybe 5–8 concurrent long-dated positions and then you're done until something resolves. `max_concurrent_positions` is your real strategy parameter. Sports recycles far faster, which is a genuine argument for weighting a small wallet toward faster-resolving markets.

**Round down, always.** Share rounding is material at small notional; round down and re-check against `min_order_usd` rather than rounding up past your cap. Anything below the floor is a skip, not a clamp.

**Never deploy the reserve.** The last 20% exists for the corrective orders the reconciler needs to issue. A bot at 100% deployment cannot fix itself.

**Exits scale against your position, not theirs.** If they sell 40% of their holding, you sell 40% of *yours*. At a 200:1 size ratio, mirroring their absolute size will flatten you on a partial trim and leave you long after a full exit. This is the one that gets written backwards.

### 3.4 `ledger/`

Per-bot virtual portfolio: capital envelope, positions, realized and unrealized PnL.

**Position lifecycle — the part that `auto_redeem: true` was hiding.**

```
open ──▶ pending_resolution ──▶ resolved ──▶ redeemed
             │                     ▲
             └──▶ disputed ────────┘
             └──▶ voided ──▶ refunded
```

Polymarket resolves through UMA's optimistic oracle. Disputes hold settlement for weeks. Markets get voided. "Resolved" and "my capital is back" are separated by an unbounded interval, and if your envelope accounting conflates them, bots will believe they have collateral they don't.

`reconcile.py` runs every 60s: diff actual on-chain positions against target-scaled expected, emit corrective intents, and advance lifecycle states. This is the component that catches every other component's bugs. Drift beyond `halt_on_reconcile_drift_usd` halts the bot rather than trading through it.

#### Redemption

`auto_redeem: true` was hiding an entire subsystem. Redemption is a **scheduled job, not a hot path** — a bot wallet with 20 resolved positions should not fire 20 transactions.

- **It's your own transaction.** Trades route through Polymarket's relayer and cost nothing; `redeemPositions` does not. Every redemption burns POL from that bot's wallet. This is why the gas float check in `doctor` matters — with no POL you accrue winning positions you cannot convert to collateral, and your envelope silently shrinks.
- **Resolved in the UI ≠ redeemable on-chain.** Check the condition's on-chain resolution status before attempting; a market showing settled to users may not yet have its payout numerators reported. Attempting early wastes gas on a revert.
- **Neg-risk uses a different path.** Multi-outcome positions redeem through the NegRisk adapter, not the plain CTF call. Route on the market's neg-risk flag.
- **Losing positions have nothing to redeem.** Write them off in the ledger; never spend gas discovering this on-chain.
- **Disputes need backoff, not retry loops.** A disputed market can sit for weeks. Retry on a slow schedule bounded by `redeem_retry_days`, and keep the position in `disputed` so its capital isn't counted as available.
- **Batch where the contract allows it**, and run on a schedule — hourly is plenty. Redemption latency has no strategic value.

Capital returns to the envelope only when redemption *confirms*, never when resolution is observed. Conflating the two is how a bot ends up sizing against money it doesn't have.

### 3.4a Paper mode

Paper is not a logging flag — it runs the entire pipeline and suppresses exactly one call: the submit. Everything upstream (selectors, guards, sizing, capital checks, ledger writes) executes identically, and everything writes to the same tables with a `mode` column so `analyze` and `replay` work without special-casing.

**Simulating the fill.** At detection, snapshot the book and walk it for your intended size, computing the VWAP you'd actually have received. That — not the target's price, and not the top of book — is your paper fill. It's the number that tells you whether the target's edge survives your latency.

**Paper bots must enforce their own envelope.** This is the part that's easy to skip and ruins everything: a paper bot with unlimited capital takes every trade and reports wonderful returns. Simulated collateral, simulated reserve, simulated `max_concurrent_positions`, and simulated resolution crediting the paper ledger when markets settle. Otherwise Phase 1's output is fiction and you'll size Phase 3 against it.

**What paper cannot capture, and must be stated in the README:**

- **Market impact.** Your order would have moved the book you're pricing against. On thin markets this is the largest single source of paper-vs-live divergence.
- **Queue position.** Any strategy involving resting orders is not simulable this way.
- **Rejections and rate limits.** Paper never gets throttled or refused.

Paper results are therefore an *optimistic* bound. Treat a strategy that's marginal on paper as losing in practice.

### 3.5 `execution/`

- `ratelimit.py` — token bucket in front of the CLOB. Saturation queues and prioritizes; it does not drop.
- `router.py` — single consumer. Build → submit → resolve.
- `clob.py` — thin `py-clob-client` wrapper. Warm HTTP/2 connection, pre-derived L2 creds, no market lookups in the hot path.

**Order lifecycle, including the state everyone forgets:**

```
built ──▶ submitted ──┬──▶ acked ──┬──▶ filled
                      │            ├──▶ partial ──▶ filled | expired
                      │            └──▶ cancelled
                      ├──▶ rejected
                      └──▶ TIMEOUT ──▶ unknown ──▶ [query] ──▶ filled | absent
```

`unknown` is the dangerous one. A submission with no response must time out into a **query-based** reconcile — never a blind retry, which is how you get an accidental double-fill.

**Idempotency lives in the database** as a unique constraint on `(bot_id, dedupe_key)` — not in application logic, where a reconnect race will eventually beat you. Note the `bot_id`: one fill legitimately produces one intent *per bot*, and if you forget it your second bot silently never trades and looks like a selector bug for a day.

**Dead-man's switch.** If a bot is SIGKILLed with resting GTC orders, they stay live on the book with nothing supervising them. Cancel-all on SIGTERM handles the graceful case; for the ungraceful one, a watchdog cancels a bot's open orders when its heartbeat goes stale. Check whether `py-clob-client` exposes the heartbeat auto-cancel the Rust client advertises — if not, implement it yourself. This is not optional for unattended operation.

### 3.6 `targets/`

The layer that keeps this working without you watching it.

- **Alpha decay monitor** — rolling 30-day PnL, hit rate, and average slippage-adjusted edge per target. Auto-pause on threshold breach. Targets stop working: they get copied to death, change strategy, or were variance all along. Without this you find out during a drawdown; with it, the system cuts its own losers.
- **Lifecycle** — dormancy detection ("whale1 hasn't traded in 21 days"), and proxy-wallet migration when a target moves address.
- ~~**Onboarding** — a new target runs in shadow inside a *live* bot for a configurable period before it's permitted to trade. Same pipeline, orders suppressed.~~ Removed 2026-07-31 at the repo owner's explicit request — new targets are active immediately. The gate existed so a not-yet-vetted wallet's first real fills weren't a blind bet with real money; that protection is gone for newly added targets now.
- **Adversarial detection** — Polymarket's leaderboards are public and your targets know copiers exist. A whale can buy, let copiers lift the book behind them, and sell into the flow they created. Flag any target whose fills are frequently followed by their own reversal within a short window. If a target's "alpha" is largely you, the correlation shows up fast.

### 3.7 `research/`

- `replay.py` — `pmex-shadow replay --from X --to Y --config candidate.yaml`. Re-runs stored fills through policy with no side effects, producing a hypothetical fill log you can diff against actual. This is the payoff for event sourcing; without it, "backtest your guards" is aspirational.
- `analyze.py` — per-target scorecards: detection latency distribution, realized slippage vs. target price by market type, hypothetical PnL, and **pairwise correlation across targets**. If two targets fire within seconds of each other on the same side, they're one signal at 2× size, and you want to know before a drawdown teaches you.
- `export.py` — CSV of all fills and realized PnL. Users will need this for taxes.

### 3.8 `control/`

One FastAPI service: reads Postgres, writes versioned config, serves a small UI. HTMX plus a lightweight chart library rather than React — no Node build step in the image, which matters for a clone-and-run repo.

**The data split that makes this work:**

| Concern | Source | Why |
|---|---|---|
| PnL, positions, fills, equity curve | **Postgres, queried directly** | exact financial state; never sampled |
| Latency, skip-rate, queue depth, socket age | **Prometheus** | operational telemetry; sampling is fine |
| Logs | **Postgres event table** | already event-sourcing; a log view is just a query |

Scraping PnL into Prometheus gives you a *sampled approximation of your money*. Financial figures come from the event store, always. This also keeps the default stack at five containers with Grafana as an optional `--profile metrics`.

**UI surface:** fleet view (per bot: mode, lag, exposure vs. envelope, today's PnL, skip rate, last fill) · bot detail (equity curve, recent fills with realized-vs-target slippage, skips by reason, live log tail) · targets (scorecards, correlation, decay status) · params.

**Parameter changes — four rules.**

1. **Config lives in a versioned Postgres table, not edited YAML.** `bots/*.yaml` seeds the initial row; the DB is authoritative thereafter. Bots watch their version and hot-reload.
2. **Validate before apply; fail to last-good.** Schema plus sanity bounds — slippage over ~10 ticks, envelope exceeding available collateral, sizing above per-market cap. A rejected config leaves the bot on its previous version and raises an alert; it never leaves a bot unconfigured.
3. **Not everything is hot-reloadable.** Guards, sizing, selectors, and (as of 2026-07-30) the target set: yes. Wallet, DB settings: restart. Mark the boundary in the schema so the UI greys them out rather than silently ignoring an edit. Target set was originally grouped in with wallet as a restart-required "identity" field, but it doesn't actually carry the same constraint: a bot's target list is just an in-process address filter recomputed from `target_stats`, with no live signing client or in-flight order state riding on it the way wallet has. Relaxed at the repo owner's explicit request once that distinction was confirmed against the code.
4. **Every change is audited** — actor, timestamp, old → new, resulting version. When a bot's behaviour changes at 3am you need to know whether a human moved a number.

### 3.9 `ops/`

`health.py` (liveness, socket age, lag histogram) · `killswitch.py` (global and per-bot halt, optional flatten) · `/metrics` for Prometheus · **`backup.py`**.

Backups are not optional here: **the event store is your cost basis.** Lose it and you don't know what you own or what you paid for it. Ship a `pg_dump` sidecar on a schedule with offsite upload, and have `doctor` check that the last successful backup is recent.

---

## 4. Data model

Core tables. Everything else is a view.

| Table | Purpose |
|---|---|
| `target_fills` | append-only; the source of truth. Unique on `dedupe_key`. |
| `intents` | policy output. Unique on `(bot_id, dedupe_key)`. Includes `Skip` rows with reason. |
| `orders` | lifecycle state machine per submission, with timestamps per transition |
| `positions` | current holdings per `(bot_id, token_id)` with lifecycle state and cost basis |
| `bot_config` | versioned; `(bot_id, version, yaml, created_by, created_at)` |
| `config_audit` | actor, diff, applied version, accept/reject |
| `target_stats` | rolling PnL, hit rate, decay status, pause state |
| `events` | structured log records; backs the UI log view |
| `heartbeats` | watcher and per-bot liveness |
| `backups` | last successful dump, checked by `doctor` |

---

## 5. CLI surface

```
pmex-shadow init                 scaffold config, migrations, secrets layout
pmex-shadow doctor [--bot NAME]  preflight — run this before anything else
pmex-shadow watcher              the shared fill stream; run one, always

pmex-shadow bot new sports --template sports
pmex-shadow bot run sports [--live]
pmex-shadow bot pause|resume|panic sports
pmex-shadow bots list|status

pmex-shadow targets add 0x… --alias whale1 --to sports --shadow-days 14
pmex-shadow targets list|pause|migrate

pmex-shadow treasury fund sports --usd 500
pmex-shadow treasury sweep politics --to funding

pmex-shadow analyze --since 14d
pmex-shadow replay --from X --to Y --config candidate.yaml
pmex-shadow export --format csv --since 2026-01-01

pmex-shadow positions | pnl [--by-bot]
pmex-shadow reconcile --dry-run
pmex-shadow compose generate     bots/*.yaml → docker-compose.bots.yml
pmex-shadow panic [--flatten]    halt every bot
```

`doctor` is the highest-value command in a clone-and-run tool. Check: RPC WS reachability and latency percentiles · CLOB reachability and RTT · exchange V2 contract/ABI match · pUSD balance and allowances · POL gas float · API cred validity · **clock skew** (skew breaks timestamped auth headers, and the failure mode is baffling) · migrations current · resolved proxy wallet per target · last backup age. Nearly every issue anyone reports will be one of these.

---

## 6. Config

`bots/*.yaml` and a shared `policy.yaml` for profiles. Secrets in `.env` and `secrets/*.env`. Never mixed.

```yaml
# policy.yaml
profiles:
  live_sports:                 # tight — books gap in <1s
    max_slippage_ticks: 1
    volatility_guard: { window_s: 5, max_ticks: 2 }
    min_copy_usd: 5
    max_position_usd: 300
  politics_longdated:          # loose — 2s costs nothing here
    max_slippage_ticks: 4
    volatility_guard: { window_s: 30, max_ticks: 8 }
    min_copy_usd: 10
    max_position_usd: 750

risk:
  global_max_exposure_usd: 5000
  max_orders_per_minute: 30
  halt_on_reconcile_drift_usd: 100

targets:
  decay:
    window_days: 30
    min_hit_rate: 0.45
    auto_pause: true
  dormancy_days: 21

exits:
  mirror_sells: true
  auto_redeem: true
  redeem_retry_days: 30        # disputed markets
```

Ship conservative defaults. Someone will clone this and run it without reading the policy section.

---

## 7. Docker

```yaml
# docker-compose.yml
services:
  db:
    image: postgres:16-alpine
    environment: { POSTGRES_DB: pmex, POSTGRES_PASSWORD: ${POSTGRES_PASSWORD} }
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s

  watcher:
    build: .
    depends_on: { db: { condition: service_healthy } }
    env_file: .env
    command: ["pmex-shadow", "watcher"]
    restart: unless-stopped

  control:
    build: .
    depends_on: [db]
    env_file: .env
    ports: ["127.0.0.1:8080:8080"]      # localhost only — see §10
    command: ["pmex-shadow", "control"]
    restart: unless-stopped

  backup:
    build: .
    depends_on: [db]
    env_file: .env
    command: ["pmex-shadow", "backup", "--schedule", "0 */6 * * *"]
    restart: unless-stopped

# docker-compose.bots.yml — generated from bots/*.yaml
  bot-sports:
    build: .
    depends_on: [watcher]
    env_file: [.env, secrets/sports.env]
    volumes: [./bots/sports.yaml:/app/bot.yaml:ro]
    command: ["pmex-shadow", "bot", "run", "sports"]
    stop_grace_period: 30s               # cancel-all on SIGTERM
    restart: unless-stopped

volumes: { pgdata: }
```

`pmex-shadow compose generate` reads `bots/*.yaml` and emits the second file, so adding a bot is: drop a YAML, regenerate, `up -d`. Each bot gets its own env file — never one shared key, or you've undone the isolation the topology exists for.

**Live-mode interlock.** Three independent things must agree: `mode: live` in config, `--live` on the command, and a non-empty `I_UNDERSTAND_THIS_TRADES_REAL_FUNDS`. Any one missing → refuse to start, naming which. For a tool strangers run against their own funds, this is not paranoia.

---

## 8. Scale envelope

Single Hetzner CPX41-class box (8 vCPU / 16 GB), US region.

| Resource | Comfortable | First hard limit | Fix |
|---|---|---|---|
| Bot processes | 20–30 | Postgres `max_connections` (default 100) | pgbouncer → 100+ |
| Target wallets | 200–500 | RPC topic-filter array size | filter in-process → thousands |
| Live book subscriptions | 200–500 | CLOB WS subscription caps | LRU eviction on target activity |
| Copied fills | 50–100/min sustained | CLOB order rate limit (**per account**) | already isolated by wallet-per-bot |
| Memory | 100–150 MB/bot | ~80 bots on 16 GB | vertical scale |
| CPU | near-idle | never the constraint | — |

**Order of failure:** Postgres connections (~25–30 bots) → IP-level rate limiting at the CLOB (accounts don't share limits, but bots share one VPS egress IP) → RPC topic-filter size → book subscription count.

Note what's absent: CPU and the language runtime. You hit four infrastructure ceilings before Python is measurable.

**The real limit is capital.** Thirty bots with meaningful envelopes is a lot of money, and copy-trading edge doesn't scale linearly — you're a price-taker into thin books, so doubling size more than doubles slippage. Infrastructure will comfortably outrun what the strategy can absorb.

---

## 9. Build order

| Phase | Deliverable | Time |
|---|---|---|
| 0 | Skeleton, Docker, migrations, `init`, `doctor`, backups | 3d |
| 1 | `watcher` + heartbeat + `watch`/`paper` + paper logger | 4d |
| — | **Observation window — run it, don't build** | 2wk |
| 2 | `policy` + `replay` + `analyze`, backtested on Phase 1 data | 4d |
| 3 | `execution` incl. order FSM + dead-man's switch, live at $5 | 5d |
| 4 | `ledger`: position lifecycle, resolution states, reconciler, exits | 5d |
| 5 | `targets`: decay monitor, onboarding, dormancy, adversarial flags | 3d |
| 6 | `control` plane + versioned config + audit | 5d |
| 7 | Metrics, killswitch, export, docs, first tagged release | 3d |

**~6 weeks part-time**, with the two-week observation window overlapping Phase 2's design work.

That gate in the middle is the point of the whole design. Phases 2–7 are cheap to build and expensive to build *wrong*, and Phase 1's data is what tells you which targets are worth copying at all. Expect one or two to look excellent on the public leaderboard and mediocre once a two-second copy delay is priced in.

---

## 10. Security posture

The control plane sits on a host holding N private keys. Defaults:

- Bind to `127.0.0.1`. Access over SSH tunnel or private network — **never a published port**.
- No default credentials. Refuse to start without an explicitly configured auth secret.
- Read-only by default; parameter writes require a separate enabling flag.
- Treasury operations are CLI-only, never in the web UI.
- Container runs non-root; secrets mounted, never baked into layers.

An open-source trading tool with a default-open dashboard on a wallet-bearing host is the single most likely way one of your users gets drained, and it will be attributed to your repo whether or not that's fair.

---

## 11. Before you publish

- **Jurisdiction.** Polymarket restricts access by region and the CLOB enforces it. Users are responsible for their own eligibility — say so plainly, and don't ship geo-bypass helpers.
- **No default targets.** Ship an empty target list. Curated addresses in the repo turn into a herd all copying the same wallet, which degrades the edge for everyone including you.
- **V2 migration.** The April 2026 CTF Exchange V2 cutover moved collateral to pUSD and broke V1 clients. Most tutorials online are pre-migration. Pin your `py-clob-client` version and state which protocol version you target.
- **Builder codes.** V2 supports per-order builder attribution. Decide your stance early — wire it up as optional sponsorship, or state clearly that the repo sets none.
- **Cost model.** RPC is the bill that surprises people; high-volume log subscriptions on managed providers aren't cheap. Publish rough monthly numbers for a 5-bot deployment.
- **Honest expectations.** Copy trading is structurally adverse-selected: you always buy after their buy moved the price. Put it in the README. A tool that overpromises attracts users who'll blame the tool.
