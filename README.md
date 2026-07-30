# pmex-shadow

Open-source copy-trading execution infrastructure for [Polymarket](https://polymarket.com).
Clone it, configure target wallets, run `docker compose up`.

> **Status: all seven phases are built.** Read this box before you do anything else.
>
> The PRD's own standing rule is: don't build the execution router (Phase 3) until
> Phase 2's policy layer has been backtested against real data from a two-week
> observation window. **That gate was deliberately overridden at the repo owner's
> explicit request**, after being told plainly what it meant — Phase 3
> (live order submission) and Phase 4 (on-chain redemption) exist here without ever
> having been backtested against real captured data, because there hasn't been a
> two-week window yet.
>
> Concretely, that means: the policy engine, sizing math, and guards are unit-tested
> against a worked example and verified end-to-end against **real live market data**
> in paper mode — but never validated against weeks of a *specific* target's actual
> behavior, which is the entire reason the gate exists. **Run this in paper mode for
> a real stretch of time and look at the numbers before you fund a live wallet.**
> The live-mode interlock (three independent conditions, FR-O-5) is fully intact and
> was never touched or tested against a funded account — going live is still a
> deliberate, multi-step action only you can take.

The full contract for what this system is and why is in
[`pmex-shadow-prd.md`](pmex-shadow-prd.md) (binding requirements) and
[`pmex-shadow-design.md`](pmex-shadow-design.md) (rationale). This README is the
practical "clone it and run it" layer on top of both. Every non-obvious protocol
detail this code relies on — contract addresses, event ABIs, API shapes, rate
limits — is verified against live sources with dates and methods in
[`docs/VERIFIED.md`](docs/VERIFIED.md); read it before touching anything that talks
to Polymarket or Polygon. It also documents a couple of real corrections found by
insisting on live verification instead of trusting a first read — worth skimming.

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
| 6 | Control plane UI, versioned config | ✅ done, all 4 screens verified in a real browser against live data |
| 7 | Metrics, killswitch, export, `compose generate` | ✅ done |

Every phase above was tested against real Polymarket/Polygon data during
development — real fills, real order books, real on-chain positions, a real browser
session against the running dashboard — not just unit tests against fixtures. What
was **not** done, and matters more: running it unattended for weeks to see how the
guards and sizing actually hold up, and exercising the live-order and redemption
paths against a funded account. Those are exactly the two things the observation gate
existed to force before Phase 3/4 got built at all.

---

## Quickstart

```bash
git clone git@github.com:Ayushjain2205/pmex-shadow.git
cd pmex-shadow
cp .env.example .env
```

Edit `.env`:
- **`PMEX_CONTROL_AUTH_SECRET`** — required, the control plane refuses to start
  without it. Generate one: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- **`POLYGON_WS_URL`** / **`POLYGON_RPC_URL`** — optional. Leave blank to run
  Data-API-only (§ below). If you set them, use a real provider (Alchemy, QuickNode,
  Chainstack, dRPC) — see [§ RPC providers](#rpc-providers-and-the-chain-path).

```bash
docker compose up -d db
docker compose run --rm migrate upgrade head
docker compose up -d watcher control backup
docker compose exec watcher pmex-shadow doctor
```

`doctor` is the first thing to run after any change — it's the highest-value command
here (design doc §5). Every check is PASS/WARN/FAIL; WARN covers expected-empty
states on a fresh clone (no bots, no backups yet), so it's safe to run immediately.

### Track a target and watch real fills come in

```bash
docker compose exec watcher pmex-shadow targets add 0x<proxy-wallet-address> --alias whale1
docker compose restart watcher   # or wait ~10s if it was idle with zero targets
docker compose exec db psql -U postgres -d pmex -c "SELECT * FROM target_fills ORDER BY id DESC LIMIT 10;"
```

**Ship with an empty target list on purpose** — this repo includes no default
targets. Curated addresses in a public repo turn into a herd all copying the same
wallet, which degrades the edge for everyone including you (design doc §11). New
targets are `active` immediately — no observation window. *(Earlier versions of
this repo ran every new target through a 14-day `shadow` probation — full pipeline,
orders suppressed — before trusting it with real orders. Removed 2026-07-31 at the
repo owner's explicit request; see PRD FR-T-4 for the tradeoff this gives up.)*

### Create a bot and run it in paper mode

```bash
docker compose exec watcher pmex-shadow bot new sports_bot1
# edit bots/sports_bot1.yaml: set targets: [whale1], selectors, policy profile
docker compose exec watcher pmex-shadow bot run sports_bot1
```

`bot new` makes a real network call: generates a fresh EOA, derives CLOB API
credentials from it, and prints a funding address. Nothing is funded automatically.
In **paper mode** (the default — `mode: paper` in the YAML), `bot run` executes the
entire pipeline — decide, size, guard, build an order — and stops only at the one
call that would leave the machine, substituting a live order-book VWAP walk instead.
Positions, PnL, and the dashboard are all real, just not backed by real money.

### Watch it in the dashboard

```bash
docker compose exec watcher pmex-shadow bot resume sports_bot1   # only if you've halted it
open http://127.0.0.1:8877/   # PMEX_CONTROL_HOST_PORT in .env, or wherever you've bound it
```

Fleet view, per-bot detail (positions, decisions, skip reasons, log tail), targets
scorecard (with an add-target-wallet form), and a params screen for hot-reloadable
settings (mode, policy profile, envelope, category selectors, target list — only
wallet requires a restart, disabled in the UI). Every change is versioned and
audited (`config_audit`).

### Going live (do not skip this section)

Three independent conditions must all be true, checked in one place
(`pmex-shadow bot run <name> --live`), or it refuses to start and names what's
missing:

1. `mode: live` in `bots/<name>.yaml`
2. the `--live` flag
3. a non-empty `I_UNDERSTAND_THIS_TRADES_REAL_FUNDS` in the environment

None of this has been exercised against a funded account. Start at `base_unit_usd: 5`
per the PRD, and only after you've watched paper mode run for real and read the
numbers yourself.

---

## RPC providers and the chain path

The chain path (`POLYGON_WS_URL`) is **optional** — for politics and pre-game
sports, Data-API-only is genuinely viable, and the system is fully functional with
`PMEX_SOURCES_CHAIN_ENABLED=0`. If you do enable it:

- **Free public WSS endpoints don't work for production.** Verified directly while
  building this: `wss://polygon-bor-rpc.publicnode.com`'s free tier subscribes fine
  but rejects `eth_getLogs` outside a small recent-block window
  (`"Archive requests require a personal token"`) — meaning backfill after any
  real outage will fail. Use a real provider (Alchemy, QuickNode, Chainstack,
  dRPC) — see `docs/VERIFIED.md` addendum items 12-13 for exactly what was tested
  and why.
- Set `POLYGON_WS_URL_FALLBACK` too. WS connections drop; the watcher fails over and
  logs it as a WARN `events` row.
- `PMEX_BACKFILL_CHUNK_BLOCKS` (default 2000) is provider-dependent — the watcher
  shrinks it automatically on an oversized-range error, but the real cap and archive
  depth depend entirely on your provider's tier.
- Redemption needs `POLYGON_RPC_URL` too, for the POL-balance safety check before
  attempting a real on-chain transaction (FR-L-10) — the redeem loop refuses to run
  without it rather than skip that check silently.

---

## CLI reference

```
pmex-shadow init                    scaffold policy.yaml, bots/, secrets/
pmex-shadow doctor [--bot NAME]     preflight checks — run this first, always

pmex-shadow bot new <name> [--import | --private-key-env VAR]
                                     generate wallet + CLOB creds, scaffold bots/<name>.yaml
                                     (--import prompts for an existing key instead of
                                     generating one — may already hold real funds, confirms first)
pmex-shadow bot run <name> [--live] consume fills, decide, execute (paper or, deliberately, live)
pmex-shadow bot resume <name>       clear a halt (reconcile drift or killswitch) — always explicit
pmex-shadow bot overlap             report bots sharing targets + selectors, combined exposure

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
                                              resting orders only, never auto-liquidates positions
pmex-shadow compose generate        bots/*.yaml -> docker-compose.bots.yml
```

`compose generate` and `doctor`/`init` run against your local checkout — install the
package locally (`pip install -e .`, or just `pip install .`) and run them from the
repo root, the same way you'd run `docker compose` itself. Don't run `compose
generate` via `docker compose exec watcher ...`: the file it writes only means
anything to the **host's** `docker compose` CLI, and a container has no way to write
back to the host filesystem outside its own mounted volumes (and no docker socket is
mounted here, on purpose — a container can't reach back and control its own
orchestrator). Bring a bot up with the file it produces:

```bash
pmex-shadow compose generate
docker compose -f docker-compose.yml -f docker-compose.bots.yml up -d bot-<name>
```

---

## Architecture in one paragraph

One shared `watcher` process ingests fills from two independent sources — a chain
subscription and a Data API poll — normalizes both into identical `TargetFill` rows
in Postgres, and fans out via `LISTEN/NOTIFY`. Each bot's own consumer reacts to
fills from its configured targets, runs them through the pure `decide()` policy
function (selectors, percentile sizing, slippage/volatility/staleness guards,
capital clamps), and — for a COPY decision — either simulates a paper fill by
walking a fresh order book, or, in live mode with the interlock satisfied, builds
and submits a real signed order through a small FSM (built → submitted → acked/
filled/rejected, with a query-based reconcile for anything that times out — never a
blind retry). A reconciler diffs on-chain positions against the ledger every 60s and
halts a bot on drift rather than trading through it; a scheduled job redeems
resolved winning positions and writes off losing ones with zero gas spent. The
control plane reads all of this straight from Postgres — never sampled, never
through Prometheus — and lets you change hot-reloadable parameters live, versioned
and audited. Full rationale in `pmex-shadow-design.md`.

---

## Security

- Control plane binds `127.0.0.1` only, refuses to start without an auth secret, and
  is read-only unless `PMEX_CONTROL_ALLOW_WRITES=1`. Never expose it on a public port.
- Private keys live in `secrets/<bot>.env`, mode `0600`, gitignored. Never in
  `bots/*.yaml` — those hold only env var *names*.
- Container runs as a non-root user (uid 1000); verified no secret ever lands in an
  image layer (the Dockerfile only `COPY`s `pmex_shadow/`, `alembic/`, `alembic.ini`
  — never `secrets/` or `.env`).
- Treasury operations don't exist in this codebase at all, in the web app or
  otherwise — there is no route or command for moving funds between bots. Rebalancing
  is manual, on-chain, by you.
- `panic --flatten` cancels resting orders; it does not auto-sell open positions.
  Flattening actual exposure is an operator decision made with eyes on the book, not
  an automated one — see `ops/killswitch.py`'s docstring for why.

## Jurisdiction and honest expectations

Polymarket restricts access by region and the CLOB enforces it. You are responsible
for your own eligibility — this repo ships no geo-bypass helpers and none will be
added. Copy trading is structurally adverse-selected: you always buy after the
target's own buy already moved the price. Paper results are an optimistic bound, not
a promise — they can't fully capture market impact, queue position, or rate limiting,
though the paper-fill simulation does walk a fresh order book at execution time
specifically to price in *some* of that latency cost. Treat a strategy that's
marginal on paper as losing in practice, and treat a strategy that's never run in
paper for more than a few hours as unvalidated, regardless of what the code does.
