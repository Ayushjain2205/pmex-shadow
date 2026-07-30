# pmex-shadow — Product Requirements Document

**Version** 1.0 · **Status** ready for implementation · **Companion doc** `pmex-shadow-design.md` (rationale; this PRD is the contract)

---

## 0. How to use this document

This PRD is written for an implementing agent. Three standing rules:

1. **Build phases in order.** Phase 1 ends in a mandatory two-week observation gate. Do not implement the router (Phase 3) before Phase 2's policy layer has been backtested against real captured data. If asked to "finish everything," build through the current phase and stop at the gate.
2. **Do not guess protocol details.** §9 lists everything that must be empirically verified against live endpoints and deployed contracts. Hallucinated ABIs and invented API field names are the single most likely failure mode here. If a fact in §9 is unverified, stop and ask rather than assume.
3. **Do not add features not in this PRD.** Non-goals are in §2 and are binding.

---

## 1. Summary

`pmex-shadow` is open-source copy-trading execution infrastructure for Polymarket. A user clones the repo, configures target wallets and bot definitions, and runs `docker compose up` on their own VPS.

The system watches target wallets' fills on-chain, applies per-bot selection and sizing policy, and mirrors qualifying trades from independently funded wallets. It ships in paper mode; live trading requires a deliberate three-part opt-in.

**Primary user**: a technically competent individual running this on a single VPS with 1–10 bots and modest capital ($500–$10,000 per bot).

**Design constraint that shapes everything**: end-to-end detection-to-order latency is ~1.5–2.5s, dominated by Polygon block time and network RTT. This is not optimizable. Correctness, capital discipline, and guard quality are where returns come from. Do not architect for low latency at the cost of correctness.

---

## 2. Non-goals

Binding. Do not implement:

- **Any latency optimization below ~50ms.** No mempool watching, no colocation logic, no custom RPC clients, no speculative pre-firing on unattributed fills.
- **Signal generation.** This system copies; it never forms its own view.
- **HA, clustering, or multi-region.** Single VPS. If the box dies, bots stop — that's acceptable, provided the dead-man's switch (FR-EXE-7) cancels resting orders.
- **A React/Node frontend.** Control plane is FastAPI + HTMX + a CDN-loaded chart library. No build step in the Docker image.
- **Geo-bypass, ToS circumvention, or ban evasion** of any kind.
- **Automated inter-bot fund transfers.** Treasury is operator-initiated, CLI-only.
- **Shared wallets between bots.** Wallet-per-bot is architectural, not configurable.
- **Kafka, Redis, Celery, or any message broker.** Fan-out is Postgres `LISTEN/NOTIFY`.

---

## 3. Environment and constraints

| Item | Value |
|---|---|
| Language | Python 3.12 |
| Async | asyncio, single process per service |
| DB | Postgres 16 |
| Chain lib | `web3.py` (AsyncWeb3, WebSocketProvider) |
| Exchange lib | `py-clob-client`, **version pinned**, V2-compatible (verify per §9) |
| Web | FastAPI + HTMX + Jinja2 |
| Metrics | `prometheus-client` |
| Config | Pydantic Settings v2 (env) + PyYAML (policy) |
| CLI | Typer |
| Migrations | Alembic |
| Packaging | Multi-stage Dockerfile, non-root user, docker compose |
| Target chain | Polygon PoS, CTF Exchange **V2** |
| Collateral | pUSD (post-April-2026 migration) |

**Decimal discipline**: all prices, sizes and monetary values use `decimal.Decimal`. Floats are prohibited in any code path touching money or prices. Enforce with a lint rule if practical.

---

## 4. Repository layout

```
pmex-shadow/
├── docker-compose.yml
├── docker-compose.bots.yml        # generated
├── Dockerfile
├── .env.example
├── policy.yaml                    # shared profiles
├── bots/                          # one YAML per bot (gitignored except .example)
├── secrets/                       # per-bot env files, 0600, gitignored
├── alembic/
├── tests/
│   ├── fixtures/                  # real captured OrderFilled logs + API responses
│   ├── unit/
│   └── integration/
└── pmex_shadow/
    ├── cli.py
    ├── config.py                  # pydantic models for bot + policy YAML
    ├── models.py                  # TargetFill, Intent, Skip, Order, Position
    ├── db.py
    ├── watcher/
    │   ├── chain.py  sweep.py  normalize.py  heartbeat.py
    ├── market/
    │   └── cache.py  classifier.py
    ├── policy/
    │   └── guards.py  sizing.py  netting.py  engine.py
    ├── ledger/
    │   └── subaccount.py  lifecycle.py  reconcile.py  redeem.py
    ├── execution/
    │   └── router.py  ratelimit.py  clob.py  deadman.py
    ├── targets/
    │   └── stats.py  decay.py  onboarding.py  adversarial.py
    ├── research/
    │   └── paper.py  replay.py  analyze.py  export.py
    ├── control/
    │   └── app.py  routes/  templates/  static/
    └── ops/
        └── health.py  killswitch.py  metrics.py  backup.py  doctor.py
```

---

## 5. Data model

Alembic-managed. `Decimal` maps to `NUMERIC`. All timestamps `TIMESTAMPTZ`.

```sql
CREATE TABLE target_fills (
  id              BIGSERIAL PRIMARY KEY,
  dedupe_key      TEXT NOT NULL UNIQUE,      -- "{tx_hash}:{log_index}" | dataapi trade id
  target          TEXT NOT NULL,             -- proxy wallet, lowercase
  token_id        TEXT NOT NULL,
  side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  price           NUMERIC(18,6) NOT NULL,
  size            NUMERIC(24,6) NOT NULL,
  notional_usd    NUMERIC(24,6) NOT NULL,
  block_number    BIGINT,
  block_ts        TIMESTAMPTZ NOT NULL,
  detected_at     TIMESTAMPTZ NOT NULL,
  source          TEXT NOT NULL CHECK (source IN ('chain','dataapi')),
  raw             JSONB NOT NULL
);
CREATE INDEX ON target_fills (target, block_ts DESC);
CREATE INDEX ON target_fills (token_id);

CREATE TABLE intents (
  id                  BIGSERIAL PRIMARY KEY,
  bot_id              TEXT NOT NULL,
  dedupe_key          TEXT NOT NULL,
  fill_id             BIGINT NOT NULL REFERENCES target_fills(id),
  decision            TEXT NOT NULL CHECK (decision IN ('COPY','SKIP')),
  skip_reason         TEXT,
  token_id            TEXT NOT NULL,
  side                TEXT NOT NULL,
  target_price        NUMERIC(18,6) NOT NULL,
  intended_price      NUMERIC(18,6),
  intended_shares     NUMERIC(24,6),
  intended_usd        NUMERIC(24,6),
  target_percentile   NUMERIC(6,2),
  size_multiplier     NUMERIC(8,4),
  book_snapshot       JSONB,
  mode                TEXT NOT NULL CHECK (mode IN ('watch','paper','live')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (bot_id, dedupe_key)
);
CREATE INDEX ON intents (bot_id, created_at DESC);
CREATE INDEX ON intents (decision, skip_reason);

CREATE TABLE orders (
  id                BIGSERIAL PRIMARY KEY,
  bot_id            TEXT NOT NULL,
  intent_id         BIGINT NOT NULL REFERENCES intents(id),
  client_order_id   TEXT NOT NULL UNIQUE,
  exchange_order_id TEXT,
  state             TEXT NOT NULL CHECK (state IN
                      ('built','submitted','acked','partial','filled',
                       'cancelled','rejected','unknown','expired')),
  token_id          TEXT NOT NULL,
  side              TEXT NOT NULL,
  limit_price       NUMERIC(18,6) NOT NULL,
  shares            NUMERIC(24,6) NOT NULL,
  filled_shares     NUMERIC(24,6) NOT NULL DEFAULT 0,
  avg_fill_price    NUMERIC(18,6),
  mode              TEXT NOT NULL,
  error             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON orders (bot_id, state);

CREATE TABLE order_transitions (
  id          BIGSERIAL PRIMARY KEY,
  order_id    BIGINT NOT NULL REFERENCES orders(id),
  from_state  TEXT,
  to_state    TEXT NOT NULL,
  detail      JSONB,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE positions (
  id                BIGSERIAL PRIMARY KEY,
  bot_id            TEXT NOT NULL,
  token_id          TEXT NOT NULL,
  shares            NUMERIC(24,6) NOT NULL DEFAULT 0,
  cost_basis_usd    NUMERIC(24,6) NOT NULL DEFAULT 0,
  realized_pnl_usd  NUMERIC(24,6) NOT NULL DEFAULT 0,
  lifecycle         TEXT NOT NULL DEFAULT 'open' CHECK (lifecycle IN
                      ('open','pending_resolution','disputed','resolved',
                       'redeemed','voided','refunded','written_off')),
  condition_id      TEXT,
  neg_risk          BOOLEAN NOT NULL DEFAULT FALSE,
  opened_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_event_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  mode              TEXT NOT NULL,
  UNIQUE (bot_id, token_id, mode)
);
CREATE INDEX ON positions (lifecycle);

CREATE TABLE bot_config (
  id          BIGSERIAL PRIMARY KEY,
  bot_id      TEXT NOT NULL,
  version     INTEGER NOT NULL,
  config      JSONB NOT NULL,
  active      BOOLEAN NOT NULL DEFAULT FALSE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (bot_id, version)
);
CREATE UNIQUE INDEX ON bot_config (bot_id) WHERE active;

CREATE TABLE config_audit (
  id            BIGSERIAL PRIMARY KEY,
  bot_id        TEXT NOT NULL,
  actor         TEXT NOT NULL,
  from_version  INTEGER,
  to_version    INTEGER,
  diff          JSONB NOT NULL,
  outcome       TEXT NOT NULL CHECK (outcome IN ('applied','rejected')),
  reason        TEXT,
  at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE target_stats (
  target              TEXT PRIMARY KEY,
  alias               TEXT,
  size_p50            NUMERIC(24,6),
  size_p60            NUMERIC(24,6),
  size_p80            NUMERIC(24,6),
  size_p95            NUMERIC(24,6),
  fills_30d           INTEGER,
  hit_rate_30d        NUMERIC(6,4),
  pnl_30d_usd         NUMERIC(24,6),
  reversal_rate       NUMERIC(6,4),         -- adversarial signal
  last_fill_at        TIMESTAMPTZ,
  status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                        ('shadow','active','paused_decay','paused_dormant','paused_manual')),
  computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE events (
  id        BIGSERIAL PRIMARY KEY,
  bot_id    TEXT,
  level     TEXT NOT NULL,
  component TEXT NOT NULL,
  message   TEXT NOT NULL,
  context   JSONB,
  at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON events (bot_id, at DESC);

CREATE TABLE heartbeats (
  service   TEXT PRIMARY KEY,        -- 'watcher' | 'bot:<name>'
  at        TIMESTAMPTZ NOT NULL,
  detail    JSONB
);

CREATE TABLE watcher_cursor (
  id                   INTEGER PRIMARY KEY CHECK (id = 1),
  last_processed_block BIGINT NOT NULL
);

CREATE TABLE backups (
  id          BIGSERIAL PRIMARY KEY,
  path        TEXT NOT NULL,
  bytes       BIGINT NOT NULL,
  succeeded   BOOLEAN NOT NULL,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Note `positions` and `intents` carry `mode`, so paper and live state coexist without special-casing in queries.

---

## 6. Core interfaces

```python
@dataclass(frozen=True)
class TargetFill:
    dedupe_key: str
    target: str
    token_id: str
    side: Side              # Enum: BUY | SELL
    price: Decimal
    size: Decimal
    notional_usd: Decimal
    block_number: int | None
    block_ts: datetime
    detected_at: datetime
    source: Literal["chain", "dataapi"]

@dataclass(frozen=True)
class BookSnapshot:
    token_id: str
    bids: list[tuple[Decimal, Decimal]]   # (price, size), best first
    asks: list[tuple[Decimal, Decimal]]
    taken_at: datetime
    def vwap_for(self, side: Side, usd: Decimal) -> tuple[Decimal, Decimal]: ...

@dataclass(frozen=True)
class Intent:
    bot_id: str
    fill: TargetFill
    token_id: str
    side: Side
    limit_price: Decimal
    shares: Decimal
    notional_usd: Decimal
    target_percentile: Decimal
    size_multiplier: Decimal

@dataclass(frozen=True)
class Skip:
    bot_id: str
    fill: TargetFill
    reason: str             # stable machine-readable token

Decision = Intent | Skip
```

**Policy engine must be a pure function.** No I/O, no clock reads, no network:

```python
def decide(
    fill: TargetFill,
    book: BookSnapshot,
    bot: BotConfig,
    ledger: LedgerState,
    target: TargetStats,
    now: datetime,
) -> Decision: ...
```

This is what makes `replay` possible. Any impurity here breaks Phase 2 and is a defect.

**Stable skip reasons** (extend, never rename — the dashboard groups on these):
`selector_category` · `selector_liquidity` · `selector_notional` · `selector_resolution_window` · `below_target_percentile` · `slippage_guard` · `volatility_guard` · `stale_fill` · `envelope_exhausted` · `max_concurrent_positions` · `global_exposure_cap` · `below_min_order` · `unknown_category` · `market_not_tradeable` · `target_paused` · `netted_out` · `bot_halted`

---

## 7. Functional requirements

### 7.1 Watcher (`FR-W-*`)

| ID | Requirement |
|---|---|
| FR-W-1 | Subscribe over WSS to `OrderFilled` logs on both the CTF Exchange V2 and NegRisk Exchange addresses, topic-filtered to the union of all configured target addresses. |
| FR-W-2 | Targets may appear as maker or taker. Cover both positions (two subscriptions or one broader filter), and verify empirically per §9. |
| FR-W-3 | Persist `last_processed_block` after each processed block. Resume from it on start — never from head. |
| FR-W-4 | On reconnect or startup gap, backfill via chunked `eth_getLogs` respecting a configurable `backfill_chunk_blocks` (default 2000). |
| FR-W-5 | Support a fallback RPC URL with automatic failover; log every failover as an `events` row at WARN. |
| FR-W-6 | `sweep.py` polls the Data API per target on `poll_interval_s` (default 30) and inserts any fills not already present. Overlap must be harmless (unique constraint). |
| FR-W-7 | The chain source must be disableable (`sources.chain.enabled: false`) with the system remaining functional on Data API alone. |
| FR-W-8 | Write a `heartbeats` row for `watcher` every 5s with the current head block and lag. |
| FR-W-9 | After each successful insert, `NOTIFY pmex_fill` with the `target_fills.id` as payload. |
| FR-W-10 | Normalize both sources into identical `TargetFill` values. Compute `notional_usd` consistently (`price × size`). |
| FR-W-11 | Store the raw decoded log/API response in `raw` JSONB for forensics. |

### 7.2 Market cache (`FR-M-*`)

| ID | Requirement |
|---|---|
| FR-M-1 | Cache per token: tick size, min order size, neg-risk flag, category/tags, event id, condition id, resolution date, tradeable status. |
| FR-M-2 | Warm on startup for all tokens with open positions; refresh on a slow background loop. |
| FR-M-3 | On cache miss in the hot path, a bot with any category selector must **skip** (`unknown_category`) and trigger an async fetch. Never block, never guess. |
| FR-M-4 | Expose `is_neg_risk(token_id)`; routing and redemption depend on it. |

### 7.3 Policy (`FR-P-*`)

| ID | Requirement |
|---|---|
| FR-P-1 | `decide()` is pure per §6. Enforced by unit tests that call it with frozen inputs and assert determinism. |
| FR-P-2 | Selectors compose with AND. Absent selector = no constraint. A bot with no selectors copies all fills from its targets. |
| FR-P-3 | Sizing uses `target_size_percentile` mode: locate the fill's notional in the target's size distribution, interpolate the multiplier from `curve`, multiply by `base_unit_usd`. |
| FR-P-4 | Fills below `min_target_size_percentile` are skipped before sizing (`below_target_percentile`). |
| FR-P-5 | Clamp order: `max_position_usd` → available envelope (`envelope_usd × (1 − reserve_pct)`) minus current exposure → `global_max_exposure_usd` across all bots (read from shared `positions`) → `max_concurrent_positions`. |
| FR-P-6 | Convert USD to shares at the limit price and **round down**. If resulting notional < `min_order_usd`, skip (`below_min_order`) — never round up. |
| FR-P-7 | Slippage guard: skip if best available price exceeds the target's fill price by more than `max_slippage_ticks`. |
| FR-P-8 | Volatility guard: skip if the book has moved more than `max_ticks` within `window_s`. Requires a short rolling book history per active token. |
| FR-P-9 | Staleness guard: skip if `now − block_ts` exceeds `max_fill_age_s`. |
| FR-P-10 | Netting: within a bot, collapse opposing intents on the same token, and handle the case where two targets of the same bot are counterparties to one fill. |
| FR-P-11 | **Exit sizing is proportional to your own position**: a target selling X% of their holding produces a sell of X% of yours. Never mirror their absolute size. |
| FR-P-12 | Every decision — including every `Skip` — is persisted to `intents`. |

### 7.4 Ledger (`FR-L-*`)

| ID | Requirement |
|---|---|
| FR-L-1 | Maintain per-`(bot_id, token_id, mode)` position with shares, cost basis, realized PnL, lifecycle state. |
| FR-L-2 | Lifecycle transitions: `open → pending_resolution → {resolved, disputed, voided}`, `resolved → redeemed`, `voided → refunded`, losing → `written_off`. |
| FR-L-3 | Capital is returned to the available envelope **only on confirmed redemption**, never on observed resolution. |
| FR-L-4 | `reconcile.py` runs every 60s: diff on-chain positions against ledger, emit corrective intents, advance lifecycle. |
| FR-L-5 | If absolute drift exceeds `halt_on_reconcile_drift_usd`, halt the bot and raise a CRITICAL event. Do not trade through drift. |
| FR-L-6 | `redeem.py` runs hourly, not in the hot path. Checks on-chain condition resolution before attempting. |
| FR-L-7 | Redemption routes through the NegRisk adapter when `neg_risk` is true, otherwise the plain CTF path. |
| FR-L-8 | Never attempt redemption on losing positions; mark `written_off` without a transaction. |
| FR-L-9 | Disputed markets retry on exponential backoff bounded by `redeem_retry_days`; capital stays excluded from the envelope while `disputed`. |
| FR-L-10 | Batch redemptions where the contract permits. Check the wallet's POL balance before attempting and raise a CRITICAL event if insufficient. |

### 7.5 Execution (`FR-EXE-*`)

| ID | Requirement |
|---|---|
| FR-EXE-1 | Single consumer per bot, reading from an `asyncio.Queue`, preserving per-token ordering. |
| FR-EXE-2 | Idempotency enforced by the `UNIQUE (bot_id, dedupe_key)` constraint on `intents`. A conflict is a normal outcome, logged at DEBUG, not an error. |
| FR-EXE-3 | Write the order row as `built` **before** submitting; update to `submitted` after. Every transition appends to `order_transitions`. |
| FR-EXE-4 | Submissions time out after `submit_timeout_s` (default 10) into state `unknown`, which triggers a query-based reconciliation against the exchange. **Never blind-retry a timed-out submission.** |
| FR-EXE-5 | Token-bucket rate limiter per bot ahead of the CLOB. On saturation, queue and prioritize by intent age; never silently drop. |
| FR-EXE-6 | On SIGTERM: stop consuming, cancel all resting orders, flush state, exit. `stop_grace_period` in compose must exceed the cancel path's worst case. |
| FR-EXE-7 | Dead-man's switch: a watchdog cancels a bot's resting orders when its heartbeat exceeds `deadman_timeout_s`. If the client library provides native heartbeat auto-cancel (verify per §9), prefer it and keep the watchdog as backup. |
| FR-EXE-8 | Bots halt themselves when the watcher heartbeat is older than `watcher_stale_s` (default 30). Log CRITICAL. Silence is treated as blindness, never as absence of activity. |
| FR-EXE-9 | Paper mode executes every step except the CLOB submit; the fill is simulated by walking `BookSnapshot` for the intended size and taking the VWAP. |
| FR-EXE-10 | Paper mode enforces simulated collateral, reserve, and `max_concurrent_positions` identically to live, and credits the paper ledger on simulated resolution. |

### 7.6 Targets (`FR-T-*`)

| ID | Requirement |
|---|---|
| FR-T-1 | Recompute `target_stats` (size percentiles, 30d PnL, hit rate, reversal rate) on a scheduled job. |
| FR-T-2 | Auto-pause a target when `hit_rate_30d` falls below `min_hit_rate` and `fills_30d` exceeds a minimum sample size. Set `status = paused_decay` and raise a WARN event. |
| FR-T-3 | Auto-pause on dormancy after `dormancy_days` without a fill. |
| FR-T-4 | *Removed 2026-07-31, repo owner's explicit request.* Previously: new targets defaulted to `status = shadow` (full pipeline, intents recorded, orders suppressed) for `shadow_days` before being trusted to trade for real. Now: new targets are `active` immediately — no observation window. The tradeoff being given up: the shadow gate existed specifically so a copier's *first* real fills for a not-yet-vetted wallet weren't a blind bet with real money — that protection no longer exists for newly added targets. The repo owner was told this plainly before the change was made. |
| FR-T-5 | Compute `reversal_rate` — the fraction of a target's fills followed by an opposing fill on the same token within `reversal_window_s`. Surface it; do not auto-act on it. |
| FR-T-6 | `pmex-shadow targets migrate <old> <new>` reassigns a target's history to a new proxy address. |

### 7.7 Control plane (`FR-C-*`)

| ID | Requirement |
|---|---|
| FR-C-1 | Financial figures (PnL, positions, fills, equity curve) are queried from Postgres directly. Prometheus is used **only** for operational telemetry. |
| FR-C-2 | Screens: fleet view, bot detail, targets, params. Log view queries the `events` table. |
| FR-C-3 | Config changes write a new `bot_config` version; bots poll their active version and hot-reload. |
| FR-C-4 | Validate against schema and sanity bounds before activation. On rejection: keep the previous active version, write a `config_audit` row with `outcome = rejected`, raise a WARN. A bot is never left unconfigured. |
| FR-C-5 | Fields not hot-reloadable (wallet, DB settings) are marked in the schema and rendered disabled with "restart required". *Amended 2026-07-30, repo owner's explicit request: originally also listed `targets`, grouped in under "identity" fields. Relaxed after confirming a bot's target set is just an in-process address filter recomputed from `target_stats` (execution/consumer.py) — nothing signing- or in-flight-order-related depends on it, unlike wallet, which is bound to a live signing client for the process lifetime. `targets` now hot-reloads like selectors/mode/policy.* |
| FR-C-6 | Every change writes `config_audit` with actor, diff, and outcome. |
| FR-C-7 | Binds to `127.0.0.1` by default. Refuses to start without `PMEX_CONTROL_AUTH_SECRET` set. No default credentials. |
| FR-C-8 | Read-only unless `PMEX_CONTROL_ALLOW_WRITES=1`. Treasury endpoints do not exist in the web app at all. |

### 7.8 Ops (`FR-O-*`)

| ID | Requirement |
|---|---|
| FR-O-1 | `doctor` checks, each pass/fail with remediation text: RPC WSS reachability + p50/p99 latency · CLOB reachability + RTT · exchange contract code present and ABI match · pUSD balance and allowances per bot · POL gas float per bot · API cred validity · **clock skew** vs NTP · Alembic head current · resolved proxy wallet per target · last successful backup age · disk free. |
| FR-O-2 | `backup.py` runs scheduled `pg_dump`, records to `backups`, supports an offsite destination. |
| FR-O-3 | Killswitch: global and per-bot, with optional `--flatten`. |
| FR-O-4 | `/metrics` exposes detection lag histogram, skip counts by reason, queue depth, order states, heartbeat ages. |
| FR-O-5 | Live-mode interlock: `mode: live` in config **and** `--live` flag **and** non-empty `I_UNDERSTAND_THIS_TRADES_REAL_FUNDS`. Missing any → refuse to start, naming which. |
| FR-O-6 | Bot names are immutable. Reject a config whose `name` differs from its `bot_id` history; provide an explicit `migrate` path instead. |
| FR-O-7 | `bots overlap` reports pairs of bots sharing targets with intersecting selectors, with estimated combined exposure. Warn at `bot new` when overlap is detected. |

---

## 8. Phases and acceptance criteria

### Phase 0 — Foundation (3d)
Skeleton, Dockerfile, compose, Alembic migrations for §5, config models, `init`, `doctor`, `backup`.

- [ ] `docker compose up` brings up db + watcher stub + control stub, all healthy
- [ ] `alembic upgrade head` creates every table in §5
- [ ] `doctor` runs all FR-O-1 checks and exits non-zero on any failure
- [ ] `bot new demo` scaffolds a YAML, derives creds, prints the funding address
- [ ] Container runs as non-root; no secret appears in any image layer
- [ ] Bot name immutability (FR-O-6) enforced at config load

### Phase 1 — Watcher + paper logger (4d), then **2-week observation gate**
- [ ] Chain watcher receives and decodes real `OrderFilled` events for a live target
- [ ] Killing the process for 10 minutes and restarting backfills the gap with zero missing fills and zero duplicates
- [ ] Data API sweep independently discovers the same fills; overlap produces no duplicate rows
- [ ] Running with `sources.chain.enabled: false` still captures fills
- [ ] Heartbeat written every 5s; a bot halts within `watcher_stale_s` of the watcher stopping
- [ ] Paper logger records, for each fill, the book snapshot and simulated VWAP fill
- [ ] **Gate: two weeks of captured data before Phase 3 begins**

### Phase 2 — Policy + replay + analyze (4d)
- [ ] `decide()` verified pure: identical inputs produce identical outputs, no I/O
- [ ] Unit coverage for every skip reason in §6
- [ ] Worked example from the design doc reproduces exactly ($10k fill at p88 → 2.0× → $50 → 79 shares)
- [ ] `replay --config candidate.yaml` reruns Phase 1 data with zero side effects
- [ ] `analyze` outputs per-target scorecards and a pairwise correlation matrix

### Phase 3 — Execution (5d)
- [ ] Order FSM implements every state and transition in §7.5; all persisted
- [ ] Injected submit timeout produces `unknown` and a query-based reconcile, never a duplicate order
- [ ] Duplicate `dedupe_key` produces exactly one order
- [ ] SIGTERM cancels all resting orders before exit
- [ ] Dead-man's switch cancels orders after a SIGKILL
- [ ] Rate limiter queues rather than drops under saturation
- [ ] Live-mode interlock verified: each of the three conditions individually blocks startup
- [ ] First live run at `base_unit_usd: 5`

### Phase 4 — Ledger (5d)
- [ ] All lifecycle transitions in FR-L-2 exercised by integration tests
- [ ] Capital returns to envelope only on confirmed redemption
- [ ] Neg-risk positions redeem via the adapter path
- [ ] Losing positions written off with no transaction
- [ ] Reconciler detects an injected drift and halts above threshold
- [ ] Insufficient POL raises CRITICAL before attempting redemption

### Phase 5 — Targets (3d)
- [ ] Percentile distribution computed from real captured fills
- [ ] Decay auto-pause fires on synthetic declining performance
- [x] ~~Shadow onboarding records intents with orders suppressed~~ — removed (FR-T-4, 2026-07-31); new targets are active immediately
- [ ] Dormancy and reversal-rate computed and surfaced

### Phase 6 — Control plane (5d)
- [ ] All four screens render from real data
- [ ] Config change creates a new version; the bot hot-reloads without restart
- [ ] Invalid config is rejected, previous version stays active, audit row written
- [ ] Non-hot-reloadable fields disabled in the UI
- [ ] Refuses to start without auth secret; read-only without the writes flag
- [ ] No treasury endpoint exists

### Phase 7 — Release (3d)
- [ ] Metrics, killswitch, CSV export
- [ ] README covering jurisdiction, empty default targets, V2 pinning, cost model, honest expectations about adverse selection
- [ ] `compose generate` produces a valid bots file from `bots/*.yaml`
- [ ] Tagged release

---

## 9. Must verify — do not assume

Every item below must be confirmed against live endpoints, deployed contracts, or installed library source **before** the code depending on it is written. Record findings in `docs/VERIFIED.md` with the date and method.

1. CTF Exchange **V2** and NegRisk Exchange addresses on Polygon.
2. The V2 `OrderFilled` event signature, and specifically **which parameters are indexed** — topic filtering only works on indexed params.
3. Whether a copied target appears in the `maker` or `taker` position (or both) for typical matched orders. Pull known historical fills for a real address and confirm.
4. `py-clob-client` version compatible with V2, and whether it exposes heartbeat / auto-cancel-on-disconnect.
5. Data API endpoint shape for user trades, including the exact `proxyWallet` field name and whether an EOA or proxy address is accepted as input.
6. Gamma (or equivalent) metadata endpoint field names for category/tags, condition id, neg-risk flag, tick size, min order size.
7. CLOB order types available (FOK/FAK/GTC/GTD) and their exact parameter names.
8. CLOB rate limits — per account and per IP — and the throttling response shape.
9. Redemption entry points: plain CTF vs NegRisk adapter, and how to read on-chain condition resolution status.
10. Whether Polymarket's relayer covers trade gas but not redemption gas, and the realistic POL cost per redemption.
11. `eth_getLogs` block-range cap on the chosen RPC provider.

---

## 10. Testing

- **Unit**: policy is pure and fully covered — every guard, every skip reason, the sizing curve interpolation, share rounding at boundaries, exit proportionality.
- **Fixtures**: build from **real captured data** — actual `OrderFilled` logs, actual Data API responses, actual book snapshots. Committed under `tests/fixtures/`. Hand-written mocks of protocol payloads are not acceptable; they encode assumptions rather than testing them.
- **Integration**: full pipeline against a local Postgres with a mocked CLOB; assert idempotency under duplicate delivery, reconnect gaps, and injected timeouts.
- **Determinism**: replay the same captured window twice and assert byte-identical intent output.
- **Chaos**: kill the watcher mid-stream, kill a bot between `built` and `submitted`, saturate the rate limiter, inject reconcile drift. Each must reach a safe state, never a duplicate order.

---

## 11. Definition of done

The repo is releasable when a stranger can clone it, run `doctor`, fund one wallet, run a paper bot for a week, read a coherent dashboard, and then enable live trading only by deliberately satisfying three separate conditions — and when killing any process at any moment leaves no orphaned orders and no duplicated positions.
