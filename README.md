# pmex-shadow

Open-source copy-trading execution infrastructure for [Polymarket](https://polymarket.com).
Clone it, configure target wallets, run `docker compose up`.

> **Status: Phase 0 + Phase 1 complete.** This is watcher-and-observation
> infrastructure, not a live trading bot yet. See [§ Build status](#build-status)
> below — there is no execution router, no policy engine, and no live-mode path.
> Nothing in this repo places an order.

The full contract for what this system is and why is in
[`pmex-shadow-prd.md`](pmex-shadow-prd.md) (binding requirements) and
[`pmex-shadow-design.md`](pmex-shadow-design.md) (rationale). This README is the
practical "clone it and run it" layer on top of both. Every non-obvious protocol
detail this code relies on — contract addresses, event ABIs, API shapes, rate
limits — is verified against live sources with dates and methods in
[`docs/VERIFIED.md`](docs/VERIFIED.md); read it before touching anything that talks
to Polymarket or Polygon.

---

## Build status

| Phase | What it is | Status |
|---|---|---|
| 0 | Skeleton, Docker, migrations, `init`/`doctor`/`backup` | ✅ done |
| 1 | Chain watcher, Data API sweep, paper-fill logger | ✅ done |
| — | **Two-week observation gate** — run it, don't build | ⏳ not started |
| 2 | Policy engine (`decide()`), `replay`, `analyze` | not started |
| 3 | Execution router, order FSM, dead-man's switch, first live trade | not started |
| 4 | Ledger, redemption, reconciliation | not started |
| 5 | Target decay/onboarding/adversarial detection | not started |
| 6 | Control plane UI, versioned config | control plane *stub* only (auth/read-only enforced, no screens) |
| 7 | Metrics, killswitch export, release | not started |

Per the PRD's own standing rule: **do not build ahead of the current phase.** Phase
2 (policy) needs real captured data from Phase 1's observation window to backtest
against — that's the entire point of the gate.

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
wallet, which degrades the edge for everyone including you (design doc §11).

### Create a bot wallet

```bash
docker compose exec watcher pmex-shadow bot new sports_bot1
```

This makes a real network call: generates a fresh EOA, derives CLOB API credentials
from it, and prints a funding address. Nothing is funded automatically — fund that
address yourself when you're ready. The private key and API credentials land in
`secrets/sports_bot1.env` at mode `0600`; edit `bots/sports_bot1.yaml` to set
targets/selectors before running the bot.

`bot run <name>` currently only supervises the shared watcher's heartbeat and halts
itself if it goes stale (FR-EXE-8) — there's no selection, sizing, or execution logic
yet; that's Phases 2-3.

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

---

## CLI reference

```
pmex-shadow init                    scaffold policy.yaml, bots/, secrets/
pmex-shadow doctor [--bot NAME]     preflight checks — run this first, always

pmex-shadow bot new <name>          generate wallet + CLOB creds, scaffold bots/<name>.yaml
pmex-shadow bot run <name>          Phase 1: watcher-heartbeat supervision only

pmex-shadow targets add <addr> [--alias NAME]
pmex-shadow targets list

pmex-shadow watcher                 shared fill stream: heartbeat + chain + sweep + paper logger
pmex-shadow control [--host] [--port]
pmex-shadow backup [--schedule CRON]
```

---

## Architecture in one paragraph

One shared `watcher` process ingests fills from two independent sources — a chain
subscription and a Data API poll — normalizes both into identical `TargetFill` rows
in Postgres, and fans out via `LISTEN/NOTIFY`. A `paper` logger reacts to every new
fill by snapshotting the live order book and recording the VWAP a copier would have
gotten filling the target's own notional right now — that's the data Phase 2's
`analyze` will use to tell whether a target's edge survives a real copy delay. There
is no bot-level selection, sizing, or order execution yet: that starts in Phase 2.
Full rationale in `pmex-shadow-design.md`.

---

## Security

- Control plane binds `127.0.0.1` only, refuses to start without an auth secret, and
  is read-only unless `PMEX_CONTROL_ALLOW_WRITES=1`. Never expose it on a public port.
- Private keys live in `secrets/<bot>.env`, mode `0600`, gitignored. Never in
  `bots/*.yaml` — those hold only env var *names*.
- Container runs as a non-root user (uid 1000); verified no secret ever lands in an
  image layer (the Dockerfile only `COPY`s `pmex_shadow/`, `alembic/`, `alembic.ini`
  — never `secrets/` or `.env`).

## Jurisdiction and honest expectations

Polymarket restricts access by region and the CLOB enforces it. You are responsible
for your own eligibility — this repo ships no geo-bypass helpers and none will be
added. Copy trading is structurally adverse-selected: you always buy after the
target's own buy already moved the price. Paper results (once Phase 1's simulation
data accumulates) are an optimistic bound, not a promise — they can't capture market
impact, queue position, or rate limiting. Treat a strategy that's marginal on paper
as losing in practice.
