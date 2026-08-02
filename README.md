# pmex-shadow

Open-source copy-trading execution infrastructure for [Polymarket](https://polymarket.com).
You pick wallets to follow; it watches their fills on-chain, decides whether each one is
worth copying, sizes it against your own capital, and executes — in paper mode by
default, live only if you deliberately unlock it.

**Setup is ~10 minutes**: clone, set one secret, `docker compose up`, add a target
wallet. [Jump to Setup →](#setup)

> ### ⚠️ Read this before you fund anything
>
> **The PRD's own safety gate was deliberately overridden at the repo owner's explicit
> request**, after being told plainly what that meant. The rule was: don't build the
> execution router (Phase 3) until the policy layer has been backtested against a
> two-week observation window of real captured data. That window never happened, and
> Phase 3 (live order submission) and Phase 4 (on-chain redemption) were built anyway.
>
> What that means concretely — the policy engine, sizing math, and guards are
> unit-tested against a worked example and verified end-to-end against **real live
> market data** in paper mode, but were never validated against weeks of a *specific*
> target's actual behavior, which is the entire reason the gate exists.
>
> **Run this in paper mode for a real stretch of time and read the numbers yourself
> before you fund a live wallet.** The live-mode interlock (three independent
> conditions, FR-O-5) is fully intact and was never touched or tested against a funded
> account — going live remains a deliberate, multi-step action only you can take.

---

## How it works

One shared **watcher** ingests every fill made by the wallets you follow, from two
independent sources (a Polygon chain subscription and a Data API poll), and writes them
to Postgres. Each **bot** you create reacts to fills from its own subset of those
wallets, runs each through a pure `decide()` policy function, and either simulates the
trade or submits a real order.

A single fill's journey (the design doc's worked example, §3.3 — `base_unit_usd: 25`,
`envelope_usd: 500`):

```
whale1 buys $10,000 of "Will X win?" at 0.62
        │
        ├─ watcher detects the fill (~2s on-chain, ~30s Data-API-only)
        │
        ├─ decide() runs:
        │    selectors      does this bot even trade this category?      ✓
        │    percentile     $10k sits at p88 of whale1's size history →  2.0× multiplier
        │    sizing         $25 base_unit × 2.0 → $50 target notional
        │    guards         she filled 0.62, best ask 0.63, tol 2 ticks  ✓
        │    clamps         max_position → envelope → global → concurrency
        │    shares         $50 ÷ 0.63 = 79.36 → 79 shares (always rounds down)
        │
        └─ COPY: limit buy, 79 shares
             paper mode → simulated against a freshly-fetched order book
             live mode  → real signed order through an FSM, never a blind retry
```

Her $10,000 became your $50 — not by dividing by 200, but because it was a
high-conviction trade for her and $50 is what conviction looks like on a $500 wallet.
The same target buying $1,200 sits around p35, below `min_target_size_percentile`, and is
skipped before sizing is even reached. Exits scale against *your* position, not theirs: if
she sells 40% of her holding, you sell 40% of yours (FR-P-11).

Every decision — including every skip, with its reason — is persisted. A reconciler diffs
on-chain positions against the ledger every 60s and halts the bot on drift rather than
trading through it. Full rationale in [`pmex-shadow-design.md`](pmex-shadow-design.md).

**What this is not:** a strategy. It ships with zero target wallets and no opinion about
whom to follow. Finding a wallet worth copying is entirely on you, and
[most aren't](#jurisdiction-and-honest-expectations).

---

## Prerequisites

| | |
|---|---|
| **Docker** with Compose v2 | `docker compose version` — everything runs in containers |
| **A Polymarket-eligible region** | the CLOB enforces this; this repo ships no bypass |
| **A Polygon RPC provider** | *optional but strongly recommended* — free tier is fine, [see below](#step-3--pick-your-data-source) |
| **Python 3.12** | *only* for `compose generate` / running the CLI on the host (`requires-python = ">=3.12,<3.13"`) |

You do **not** need a funded wallet to run everything below. Paper mode is the default
and touches no money.

---

## Setup

### Step 1 — Clone and configure

```bash
git clone git@github.com:Ayushjain2205/pmex-shadow.git
cd pmex-shadow
cp .env.example .env
```

Generate the one required secret:

```bash
python3 -c "import secrets; print('PMEX_CONTROL_AUTH_SECRET=' + secrets.token_urlsafe(32))"
```

Paste it into `.env`. The control plane **refuses to start** without it (FR-C-7) — this
is the only value you must set to get running. Everything else in `.env.example` has a
working default.

### Step 2 — Bring up the database

```bash
docker compose up -d db
docker compose run --rm migrate upgrade head
```

The second command creates every table and exits. It's a one-shot job, not a service.

### Step 3 — Pick your data source

**This is the one decision that will make or break your first run**, so make it now
rather than debugging it later.

|  | Data API only | Chain + Data API *(recommended)* |
|---|---|---|
| Setup | nothing, works out of the box | free Alchemy/QuickNode account |
| Fill detection | ~30s (polling interval) | ~2s |
| Works with default `tight` profile? | **No — see below** | Yes |

The shipped `tight` and `flat` profiles use `max_fill_age_s: 5`. On Data-API-only, no
fill is ever discovered sooner than the 30s poll, so **every single fill is skipped as
`stale_fill`, unconditionally** — not occasionally. Your dashboard will show fills
arriving and zero copies, and nothing is broken. You have three options: enable the
chain path, switch the bot to the `loose` profile (`max_fill_age_s: 30`), or raise
`max_fill_age_s` in `policy.yaml`.

**To enable the chain path (recommended):**

1. Sign up at [alchemy.com](https://alchemy.com) — free tier is enough.
2. Create an App → Chain **Polygon**, Network **Polygon Mainnet**.
3. Open the app → **View Key** → copy both the **WebSocket (WSS)** and **HTTPS** URLs.
4. Put them in `.env`:
   ```bash
   POLYGON_WS_URL=wss://polygon-mainnet.g.alchemy.com/v2/<your-api-key>
   POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/<your-api-key>
   PMEX_SOURCES_CHAIN_ENABLED=1
   ```

QuickNode, Chainstack, and dRPC work identically. **Free public endpoints do not work** —
see [RPC provider notes](#rpc-provider-notes) for exactly what was tested and why.

**To stay on Data-API-only:** set `PMEX_SOURCES_CHAIN_ENABLED=0` and use the `loose`
profile on your bots.

### Step 4 — Start the services

```bash
docker compose up -d watcher control backup targets-recompute
docker compose exec watcher pmex-shadow doctor
```

> **Do not omit `targets-recompute`.** It is the only writer of `target_stats` — the size
> percentiles that sizing reads. Without it those columns stay `0`, every fill scores
> above all four breakpoints, and **every trade gets sized at the curve's maximum
> multiplier** instead of scaling with conviction. This is not theoretical; it silently
> happened during development. See the comment on the service in
> [`docker-compose.yml`](docker-compose.yml).

`doctor` is the highest-value command in this repo (design doc §5) — run it after any
change. Each check prints `[PASS]`, `[WARN]`, or `[FAIL]` with remediation text, and the
command exits non-zero if anything FAILs. On a fresh clone, WARNs about "no bots" and
"no backups yet" are expected and safe to ignore.

### Step 5 — Follow a wallet

```bash
docker compose exec watcher pmex-shadow targets add 0x<proxy-wallet-address> --alias whale1
docker compose restart watcher
```

Confirm fills are actually landing (give it a few minutes — the wallet has to trade):

```bash
docker compose exec db psql -U postgres -d pmex \
  -c "SELECT at, target, side, price, notional_usd FROM target_fills ORDER BY id DESC LIMIT 10;"
```

Use the wallet's **proxy address** (the one that appears on-chain in Polymarket trades),
not an EOA. `doctor` resolves and checks this for every target.

**This repo ships with an empty target list on purpose.** Curated addresses in a public
repo turn into a herd all copying the same wallet, degrading the edge for everyone
including you (design doc §11). New targets go `active` immediately — there is no
observation window. *(Earlier versions ran every new target through a 14-day `shadow`
probation — full pipeline, orders suppressed — before trusting it with real orders.
Removed 2026-07-31 at the repo owner's explicit request; PRD FR-T-4 records the tradeoff
this gives up.)*

### Step 6 — Create a bot and run it

```bash
docker compose exec watcher pmex-shadow bot new mybot1
```

This makes a real network call: it generates a fresh EOA, derives CLOB API credentials
from it, writes `bots/mybot1.yaml` plus `secrets/mybot1.env` (mode `0600`), and prints a
funding address. **Nothing is funded automatically.**

Edit `bots/mybot1.yaml` — at minimum, point it at your target:

```yaml
name: mybot1             # immutable: must match the filename (FR-O-6)
mode: paper              # watch | paper | live — paper touches no money
wallet:                  # written by `bot new`; holds env var *names*, never keys
  funder_env: MYBOT1_FUNDER
  pk_env: MYBOT1_PK
targets: [whale1]        # aliases from `targets add`
policy:
  profile: tight         # tight | loose | flat — see policy.yaml
selectors: {}            # empty = copy every fill from these targets
risk:
  envelope_usd: '500'    # your simulated capital for this bot
```

Two things that will bite you if you hand-edit this file:

- **Money and price values must be quoted strings**, not YAML floats. `'500'` and `500`
  are fine; `500.0` is rejected at load. Decimal discipline is enforced, not advisory —
  binary floats have no business holding money (PRD §3).
- **`name` is immutable** and must match the filename. Renaming it in place forks the
  bot's own history, so it's rejected; use `targets migrate` for a real rename.

Optional `selectors` narrow what the bot copies — `categories`,
`min_book_liquidity_usd`, `min_target_notional_usd`, `max_time_to_resolution_days`,
plus the `deny`/`allow` rules below. They compose with AND, and an absent selector is
no constraint (FR-P-2).

`deny` and `allow` filter on *market identity*, which is what you need when a target
trades several markets you don't want equally. A wallet running BTC, SOL and XRP
5-minute markets can't be split by `categories` (all three share one) or by event id
(each recurrence mints a new one), so rules match a bag of resolved attributes
instead — `asset`, `duration`, `series`, `tag`, `event_id`, `slug`, `category`:

```yaml
selectors:
  deny:
    - asset: sol                      # copy their BTC and XRP, skip SOL
  allow:
    - {tag: crypto-prices, duration: 5m}
```

Keys within one rule are ANDed, values within a key ORed, and several rules ORed.
Deny wins over allow. A rule can only match a market that *has* the attribute, which
gives the two the right asymmetry across market families: `{asset: sol}` is inert
against a weather market (no `asset` to match) as a deny, but excludes it as an
allow, since an allowlist should only admit what it can positively identify.

Prefer exact values over patterns. A deny rule that stops matching — because a slug
was renamed — fails open: you silently resume copying what you meant to exclude. For
families with no structured key there's `slug: {re: '...'}`, compiled at load and
matched with `fullmatch`, so `sol` will not match `solana-...`.

Rule attributes are validated at load, so a typo is a startup error rather than a
filter that quietly never fires. `event_ids` is **not implemented** and never was —
setting it is now a load error pointing at `allow: [{event_id: ...}]`.

One cross-family trap: `max_slippage_ticks` and `volatility_guard.max_ticks` are in
*ticks*, and tick size varies by family — 0.01 on crypto up/down markets, 0.001 on
daily weather. The `tight` profile is 10x stricter on weather than on crypto and will
skip almost everything on `slippage_guard`, which reads as a broken filter but isn't.
Use a separate profile per family.

Then run it:

```bash
docker compose exec watcher pmex-shadow bot run mybot1
```

In **paper mode** this executes the entire pipeline — decide, size, guard, build the
order — and stops only at the one call that would leave the machine, substituting a walk
of a freshly-fetched order book. Positions, PnL, and the dashboard are all real; only the
money isn't.

For a long-lived bot, generate a proper compose service instead of `exec` — see
[`compose generate`](#running-bots-as-services).

### Step 7 — Open the dashboard

```bash
open http://127.0.0.1:8877/
```

Four top-level screens — **Fleet** (all bots, status, portfolio), **Targets**
(scorecards + add-wallet form), **Analysis** (copy-trade latency), **Logs** (filterable
`events`) — plus **bot detail** (positions, every decision with its skip reason, equity
curve, log tail) and a **Params** screen per bot for hot-reloadable settings. Every config
change is versioned and audited in `config_audit`.

Only `wallet` requires a restart to change; mode, policy profile, envelope, selectors, and
the target list all hot-reload (FR-C-5). Non-reloadable fields render disabled in the UI.

The dashboard is **read-only** until you set `PMEX_CONTROL_ALLOW_WRITES=1` in `.env`
(FR-C-8). The port is `PMEX_CONTROL_HOST_PORT`, bound to `127.0.0.1` only.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Control plane won't start | `PMEX_CONTROL_AUTH_SECRET` empty | Set it in `.env` (Step 1) — FR-C-7, by design |
| Fills arriving, **zero copies**, all skips say `stale_fill` | Data-API-only + `max_fill_age_s: 5` | [Step 3](#step-3--pick-your-data-source) — enable chain path or use `loose` |
| No fills at all in `target_fills` | Watcher started before the target existed | `docker compose restart watcher` |
| Every trade sized at max multiplier; Targets page columns all blank | `targets-recompute` not running | `docker compose up -d targets-recompute` |
| Skips say `unknown_category` | Market metadata cache miss | Expected on first sight of a token; it fetches async and resolves (FR-M-3 — it skips rather than guesses) |
| Skips say `below_min_order` | Sized notional fell under `min_order_usd` | Raise `base_unit_usd`, or accept it — it never rounds up (FR-P-6) |
| Backfill fails after an outage | Free public RPC rejects `eth_getLogs` | Use a real provider — [notes below](#rpc-provider-notes) |
| Bot halted itself, refuses to trade | Reconcile drift or killswitch | Investigate first, then `pmex-shadow bot resume <name>` — always explicit |
| `compose generate` output does nothing | Ran it inside a container | Run it on the **host** — [see below](#running-bots-as-services) |

Whatever the symptom, run `pmex-shadow doctor` first — it checks RPC reachability and
latency, CLOB RTT, contract ABI match, balances and allowances, clock skew, Alembic head,
target proxy resolution, backup age, and disk free.

### Why a fill wasn't copied

Every decision is persisted, including every skip, with structured detail (FR-P-12) —
the bot detail screen colors them by category. If copies aren't happening, this table is
where you look:

| Category | Reasons | What it means |
|---|---|---|
| **timing** | `stale_fill` | You saw it too late. The #1 cause on Data-API-only. |
| **slippage** | `slippage_guard` | Price already moved past `max_slippage_ticks` — the guard working as intended. |
| **chaos** | `volatility_guard` | Book moved more than `max_ticks` inside `window_s`. |
| **capital** | `envelope_exhausted`, `global_exposure_cap` | Out of room. Raise `envelope_usd` or `global_max_exposure_usd`. |
| **position limit** | `max_concurrent_positions` | Slots full. |
| **halted** | `bot_halted`, `target_paused` | Killswitch, reconcile drift, or decay auto-pause. |
| **filtered** | `selector_*`, `unknown_category`, `market_not_tradeable`, `below_target_percentile`, `below_min_order`, `no_position_to_exit`, `netted_out` | Working as configured — a selector or sizing rule excluded it. |
| **data gap** | `no_orderbook`, `no_market_meta`, `target_not_registered` | A lookup came back empty, so `decide()` never ran. Not a verdict — usually transient. |

The distinction that matters when debugging: **filtered** means your config said no,
**data gap** means the system couldn't tell. Everything else is a guard firing.

---

## Going live (do not skip this section)

Three independent conditions must **all** be true, or `bot run --live` refuses to start
and names exactly which one is missing:

1. `mode: live` in `bots/<name>.yaml`
2. the `--live` flag on the command
3. a non-empty `I_UNDERSTAND_THIS_TRADES_REAL_FUNDS` in the environment

None of this has ever been exercised against a funded account. Before you consider it:
run paper mode for weeks, not hours; read the numbers; then start at `base_unit_usd: 5`
per the PRD.

---

## Running bots as services

`bot run` via `docker compose exec` is fine for a first look, but a real deployment wants
each bot as its own service. `compose generate` writes one from your `bots/*.yaml`:

```bash
pmex-shadow compose generate
docker compose -f docker-compose.yml -f docker-compose.bots.yml up -d bot-mybot1
```

Run this **on the host**, not inside a container (`pip install -e .` first). The file it
writes only means anything to the host's `docker compose` CLI, and a container can't
write back to the host filesystem outside its mounted volumes — no docker socket is
mounted here, on purpose, so a container can't reach back and control its own
orchestrator.

---

## CLI reference

```
pmex-shadow init                    scaffold policy.yaml, bots/, secrets/
pmex-shadow doctor [--bot NAME]     preflight checks — run this first, always

pmex-shadow bot new <name> [--import | --private-key-env VAR]
                                    generate wallet + CLOB creds, scaffold bots/<name>.yaml
                                    (--import uses an existing key instead of generating
                                    one — may already hold real funds, confirms first)
pmex-shadow bot run <name> [--live] consume fills, decide, execute (paper or, deliberately, live)
pmex-shadow bot resume <name>       clear a halt (reconcile drift or killswitch)
pmex-shadow bot overlap             bots sharing targets + selectors, combined exposure

pmex-shadow targets add <addr> [--alias NAME]
pmex-shadow targets list|pause|resume|migrate
pmex-shadow targets recompute [--schedule CRON]   stats, decay/dormancy auto-pause

pmex-shadow watcher                 shared fill stream: heartbeat + chain + sweep + paper logger
pmex-shadow control [--host] [--port]
pmex-shadow backup [--schedule CRON]
pmex-shadow replay --config <candidate.yaml> --from <ts> --to <ts>
pmex-shadow analyze [--since 14d]
pmex-shadow export --kind fills|pnl [--since <date>] [--output FILE]
pmex-shadow panic [--bot NAME] [--flatten]   halt every bot, or one; --flatten cancels
                                             resting orders only, never auto-liquidates
pmex-shadow compose generate        bots/*.yaml -> docker-compose.bots.yml (run on host)
```

---

## RPC provider notes

The chain path is **optional** (FR-W-7) — for politics and pre-game sports, Data-API-only
is genuinely viable, with the `max_fill_age_s` caveat in
[Step 3](#step-3--pick-your-data-source).

- **Free public WSS endpoints don't work for production.** Verified directly while
  building this: `wss://polygon-bor-rpc.publicnode.com`'s free tier subscribes fine but
  rejects `eth_getLogs` outside a small recent-block window
  (`"Archive requests require a personal token"`) — so backfill after any real outage
  fails. See `docs/VERIFIED.md` addendum items 12–13.
- Set `POLYGON_WS_URL_FALLBACK` too. WS connections drop; the watcher fails over and logs
  a WARN `events` row.
- `PMEX_BACKFILL_CHUNK_BLOCKS` (default 2000) is provider-dependent. The watcher shrinks
  it automatically on an oversized-range error, but the real cap and archive depth depend
  on your provider's tier.
- Redemption needs `POLYGON_RPC_URL` for the POL-balance check before attempting a real
  transaction (FR-L-10). The redeem loop refuses to run without it rather than silently
  skip that check.

---

## Build status

| Phase | What it is | Status |
|---|---|---|
| 0 | Skeleton, Docker, migrations, `init`/`doctor`/`backup` | ✅ done, verified in Docker |
| 1 | Chain watcher, Data API sweep, paper-fill logger | ✅ done, verified against live fills |
| 2 | Policy engine (`decide()`), `replay`, `analyze` | ✅ done, worked example reproduced exactly |
| 3 | Execution router, order FSM, dead-man's switch | ✅ done, paper mode verified live; live-mode order submission never exercised against real funds |
| 4 | Ledger, redemption, reconciliation | ✅ done, reconcile verified against real on-chain positions; redemption never executed for real |
| 5 | Target decay/onboarding/adversarial detection | ✅ done, decay auto-pause verified actually firing on live data |
| 6 | Control plane UI, versioned config | ✅ done, all screens verified in a real browser against live data |
| 7 | Metrics, killswitch, export, `compose generate` | ✅ done |

Every phase was tested against real Polymarket/Polygon data during development — real
fills, real order books, real on-chain positions, a real browser session against the
running dashboard — not just unit tests against fixtures. What was **not** done, and
matters more: running it unattended for weeks to see how the guards and sizing actually
hold up, and exercising the live-order and redemption paths against a funded account.
Those are precisely the two things the observation gate existed to force.

---

## Documentation map

| Document | What's in it |
|---|---|
| **README.md** (this file) | clone it and run it |
| [`pmex-shadow-prd.md`](pmex-shadow-prd.md) | binding requirements — every `FR-*` referenced here |
| [`pmex-shadow-design.md`](pmex-shadow-design.md) | rationale — why each decision went the way it did |
| [`docs/VERIFIED.md`](docs/VERIFIED.md) | every protocol detail (contract addresses, event ABIs, API shapes, rate limits) verified against live sources, with dates and methods |

Read `docs/VERIFIED.md` before touching anything that talks to Polymarket or Polygon. It
also documents real corrections found by insisting on live verification instead of
trusting a first read — worth skimming even if you're not changing that code.

---

## Security

- Control plane binds `127.0.0.1` only, refuses to start without an auth secret, and is
  read-only unless `PMEX_CONTROL_ALLOW_WRITES=1`. Never expose it on a public port.
- Private keys live in `secrets/<bot>.env`, mode `0600`, gitignored. Never in
  `bots/*.yaml` — those hold only env var *names*.
- Container runs as non-root (uid 1000); no secret ever lands in an image layer (the
  Dockerfile only `COPY`s `pmex_shadow/`, `alembic/`, `alembic.ini` — never `secrets/` or
  `.env`).
- Treasury operations don't exist in this codebase at all — no route, no command for
  moving funds between bots. Rebalancing is manual, on-chain, by you.
- `panic --flatten` cancels resting orders; it does **not** auto-sell open positions.
  Flattening real exposure is an operator decision made with eyes on the book, not an
  automated one — see `ops/killswitch.py`'s docstring for why.

## Jurisdiction and honest expectations

Polymarket restricts access by region and the CLOB enforces it. You are responsible for
your own eligibility — this repo ships no geo-bypass helpers and none will be added.

Copy trading is structurally adverse-selected: you always buy *after* the target's own buy
already moved the price. Paper results are an optimistic bound, not a promise — they can't
fully capture market impact, queue position, or rate limiting, though the paper-fill
simulation does walk a fresh order book at execution time specifically to price in *some*
of that latency cost. Treat a strategy that's marginal on paper as losing in practice, and
treat a strategy that's never run in paper for more than a few hours as unvalidated,
regardless of what the code does.
