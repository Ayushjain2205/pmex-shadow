from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import stat
from pathlib import Path

import typer
import yaml

from pmex_shadow.config import Settings, load_bot_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

app = typer.Typer(add_completion=False, help="pmex-shadow — copy-trading execution infrastructure for Polymarket")

BOTS_DIR = Path("bots")
SECRETS_DIR = Path("secrets")
POLICY_FILE = Path("policy.yaml")


@app.command()
def init() -> None:
    """Scaffold config, migrations layout, and secrets dir for a fresh clone."""
    BOTS_DIR.mkdir(exist_ok=True)
    SECRETS_DIR.mkdir(exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)

    if not POLICY_FILE.exists():
        POLICY_FILE.write_text(_DEFAULT_POLICY_YAML)
        typer.echo(f"wrote {POLICY_FILE}")

    env_example = Path(".env.example")
    if not Path(".env").exists() and env_example.exists():
        typer.echo("Copy .env.example to .env and fill in secrets before running `doctor`.")

    typer.echo("pmex-shadow initialized. Next: `pmex-shadow doctor`, then `pmex-shadow bot new <name>`.")


@app.command()
def doctor(bot: str | None = typer.Option(None, "--bot", help="limit checks to one bot (unused in Phase 0)")) -> None:
    """Preflight checks (FR-O-1). Exits non-zero if any check FAILs."""
    from pmex_shadow.ops.doctor import run_all_checks

    settings = Settings()
    results = asyncio.run(run_all_checks(settings, BOTS_DIR))

    any_fail = False
    for r in results:
        color = {"PASS": typer.colors.GREEN, "WARN": typer.colors.YELLOW, "FAIL": typer.colors.RED}[r.status]
        typer.secho(f"[{r.status:4}] {r.name:32} {r.detail}", fg=color)
        if r.remediation:
            typer.echo(f"       -> {r.remediation}")
        if r.status == "FAIL":
            any_fail = True

    if any_fail:
        typer.secho("doctor: one or more checks FAILED", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho("doctor: all checks passed (or are expected-empty WARNs)", fg=typer.colors.GREEN)


bot_app = typer.Typer(help="Manage bots")
app.add_typer(bot_app, name="bot")


@bot_app.command("new")
def bot_new(
    name: str,
    template: str = typer.Option("default", "--template"),
) -> None:
    """Scaffold bots/<name>.yaml, derive a wallet + CLOB creds, print the funding address."""
    from pmex_shadow.ops.wallets import provision_bot_wallet

    BOTS_DIR.mkdir(exist_ok=True)
    SECRETS_DIR.mkdir(exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)

    bot_yaml_path = BOTS_DIR / f"{name}.yaml"
    if bot_yaml_path.exists():
        typer.secho(f"{bot_yaml_path} already exists — refusing to overwrite", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.echo(f"Deriving wallet and CLOB credentials for '{name}' (live network call)...")
    try:
        wallet = asyncio.run(provision_bot_wallet())
    except Exception as exc:
        typer.secho(f"credential derivation failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    funder_env = f"{name.upper()}_FUNDER"
    pk_env = f"{name.upper()}_PK"

    secret_path = SECRETS_DIR / f"{name}.env"
    secret_path.write_text(
        "\n".join(
            [
                f"{pk_env}={wallet.private_key}",
                f"{funder_env}={wallet.funding_address}",
                f"{name.upper()}_API_KEY={wallet.api_key}",
                f"{name.upper()}_API_SECRET={wallet.api_secret}",
                f"{name.upper()}_API_PASSPHRASE={wallet.api_passphrase}",
                "",
            ]
        )
    )
    os.chmod(secret_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600

    bot_yaml = {
        "name": name,
        "mode": "paper",
        "wallet": {"funder_env": funder_env, "pk_env": pk_env},
        "selectors": {},
        "targets": [],
        "policy": {"profile": "tight"},
        "risk": {"envelope_usd": "500"},
    }
    bot_yaml_path.write_text(yaml.safe_dump(bot_yaml, sort_keys=False))

    try:
        load_bot_config(bot_yaml_path)
    except Exception as exc:
        typer.secho(f"scaffolded config failed its own validation: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.secho(f"wrote {bot_yaml_path}", fg=typer.colors.GREEN)
    typer.secho(f"wrote {secret_path} (mode 0600)", fg=typer.colors.GREEN)
    typer.secho(f"wallet type: {wallet.wallet_type}", fg=typer.colors.GREEN)
    typer.secho(f"FUND THIS ADDRESS: {wallet.funding_address}", fg=typer.colors.CYAN, bold=True)
    typer.echo("Edit bots/<name>.yaml to set targets/selectors, then `pmex-shadow doctor --bot <name>`.")


@bot_app.command("run")
def bot_run(name: str, live: bool = typer.Option(False, "--live")) -> None:
    """Run a bot. Phase 1: watcher-heartbeat supervision only (FR-EXE-8) — halts
    itself when the shared watcher goes stale. Selection/sizing/execution ship in
    Phases 2-3; this is deliberately not a full trading loop yet.
    """
    import asyncpg

    from pmex_shadow.ops.health import heartbeat_age

    if live:
        typer.secho("--live is not available before Phase 3's execution router exists", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    bot_yaml_path = BOTS_DIR / f"{name}.yaml"
    if not bot_yaml_path.exists():
        typer.secho(f"{bot_yaml_path} not found — run `pmex-shadow bot new {name}` first", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    cfg = load_bot_config(bot_yaml_path)
    settings = Settings()

    async def _run() -> None:
        conn = await asyncpg.connect(settings.database_url)
        try:
            typer.echo(f"bot '{cfg.name}' supervising watcher heartbeat (halts if stale > {settings.watcher_stale_s}s)")
            while True:
                age = await heartbeat_age(conn, "watcher")
                if age is None:
                    await conn.execute(
                        "INSERT INTO events (bot_id, level, component, message) VALUES ($1, 'CRITICAL', 'bot.health', 'watcher has never reported a heartbeat')",
                        cfg.name,
                    )
                    typer.secho("HALTED: watcher has never reported a heartbeat", fg=typer.colors.RED)
                    raise typer.Exit(code=1)
                if age.total_seconds() > settings.watcher_stale_s:
                    await conn.execute(
                        "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'CRITICAL', 'bot.health', 'watcher heartbeat stale, halting', $2)",
                        cfg.name, f'{{"age_s": {age.total_seconds():.1f}}}',
                    )
                    typer.secho(f"HALTED: watcher heartbeat is {age.total_seconds():.1f}s old (limit {settings.watcher_stale_s}s)", fg=typer.colors.RED)
                    raise typer.Exit(code=1)
                await asyncio.sleep(5)
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def watcher() -> None:
    """Shared fill stream: heartbeat + chain subscription + Data API sweep + paper
    logger, run concurrently (§2 design doc — one watcher, shared across all bots).
    """
    from pmex_shadow.watcher.chain import run_chain_watcher
    from pmex_shadow.watcher.heartbeat import run_heartbeat_loop
    from pmex_shadow.watcher.sweep import run_sweep_loop
    from pmex_shadow.research.paper import run_paper_logger

    settings = Settings()

    async def _main() -> None:
        tasks = [asyncio.create_task(run_heartbeat_loop(settings.database_url, "watcher", lambda: {
            "chain_enabled": settings.sources_chain_enabled,
            "dataapi_enabled": settings.sources_dataapi_enabled,
        }))]

        if settings.sources_chain_enabled:
            if not settings.polygon_ws_url:
                typer.secho("PMEX_SOURCES_CHAIN_ENABLED=1 but POLYGON_WS_URL is not set — chain source disabled", fg=typer.colors.YELLOW)
            else:
                tasks.append(asyncio.create_task(run_chain_watcher(settings)))
        if settings.sources_dataapi_enabled:
            tasks.append(asyncio.create_task(
                run_sweep_loop(settings.database_url, settings.data_api_base_url, settings.dataapi_poll_interval_s)
            ))
        tasks.append(asyncio.create_task(run_paper_logger(settings.database_url, settings.clob_base_url)))

        typer.echo(f"watcher started: chain={settings.sources_chain_enabled and bool(settings.polygon_ws_url)} dataapi={settings.sources_dataapi_enabled}")
        await asyncio.gather(*tasks)

    asyncio.run(_main())


targets_app = typer.Typer(help="Manage watched targets")
app.add_typer(targets_app, name="targets")


@targets_app.command("add")
def targets_add(
    address: str,
    alias: str | None = typer.Option(None, "--alias"),
) -> None:
    """Register a target wallet address for the watcher to track (target_stats)."""
    import asyncpg

    settings = Settings()

    async def _add() -> None:
        conn = await asyncpg.connect(settings.database_url)
        try:
            await conn.execute(
                """
                INSERT INTO target_stats (target, alias, status)
                VALUES ($1, $2, 'shadow')
                ON CONFLICT (target) DO UPDATE SET alias = COALESCE(EXCLUDED.alias, target_stats.alias)
                """,
                address.lower(), alias,
            )
        finally:
            await conn.close()

    asyncio.run(_add())
    typer.secho(f"tracking {address.lower()}" + (f" (alias: {alias})" if alias else ""), fg=typer.colors.GREEN)
    typer.echo("Restart the watcher (or wait for its next reconnect/sweep cycle) to pick it up.")


@targets_app.command("list")
def targets_list() -> None:
    """List currently tracked targets."""
    import asyncpg

    settings = Settings()

    async def _list():
        conn = await asyncpg.connect(settings.database_url)
        try:
            return await conn.fetch("SELECT target, alias, status, last_fill_at FROM target_stats ORDER BY target")
        finally:
            await conn.close()

    rows = asyncio.run(_list())
    if not rows:
        typer.echo("no targets tracked yet — `pmex-shadow targets add <address>`")
        return
    for r in rows:
        typer.echo(f"{r['target']}  alias={r['alias'] or '-'}  status={r['status']}  last_fill={r['last_fill_at'] or 'never'}")


@app.command()
def replay(
    config: Path = typer.Option(..., "--config", help="candidate bot YAML"),
    from_: str = typer.Option(..., "--from", help="ISO date/datetime"),
    to: str = typer.Option(..., "--to", help="ISO date/datetime"),
) -> None:
    """Rerun stored fills through policy with zero side effects (§10 Determinism)."""
    import dateutil.parser

    from pmex_shadow.config import load_policy_file
    from pmex_shadow.models import Intent, Skip
    from pmex_shadow.research.replay import run_replay

    settings = Settings()
    bot = load_bot_config(config)
    policy_file = load_policy_file(POLICY_FILE)
    from_ts = dateutil.parser.isoparse(from_)
    to_ts = dateutil.parser.isoparse(to)

    decisions = asyncio.run(run_replay(settings.database_url, settings.gamma_api_base_url, bot, policy_file, from_ts, to_ts))

    intents = [d for d in decisions if isinstance(d, Intent)]
    skips = [d for d in decisions if isinstance(d, Skip)]
    typer.echo(f"{len(decisions)} decisions: {len(intents)} COPY, {len(skips)} SKIP")
    by_reason: dict[str, int] = {}
    for s in skips:
        by_reason[s.reason] = by_reason.get(s.reason, 0) + 1
    for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  skip[{reason}]: {count}")
    if intents:
        total_usd = sum((i.notional_usd for i in intents), start=intents[0].notional_usd * 0)
        typer.echo(f"  total hypothetical notional: ${total_usd}")


@app.command()
def analyze(
    since: str = typer.Option("14d", "--since", help="e.g. 14d, or an ISO date"),
) -> None:
    """Per-target scorecards and pairwise correlation (design doc §3.7)."""
    import asyncpg

    from pmex_shadow.research.analyze import pairwise_correlation, target_scorecards

    settings = Settings()
    since_dt = _parse_since(since)

    async def _run():
        conn = await asyncpg.connect(settings.database_url)
        try:
            return await target_scorecards(conn, since_dt), await pairwise_correlation(conn, since_dt)
        finally:
            await conn.close()

    scorecards, correlations = asyncio.run(_run())

    typer.echo(f"=== Target scorecards (since {since_dt.isoformat()}) ===")
    for sc in scorecards:
        typer.echo(
            f"{sc.alias or sc.target}  fills={sc.fills}  status={sc.status}  "
            f"avg_latency={f'{sc.avg_detection_latency_s:.1f}s' if sc.avg_detection_latency_s else 'n/a'}  "
            f"avg_slippage={sc.avg_slippage_vs_target if sc.avg_slippage_vs_target is not None else 'n/a'}  "
            f"hit_rate_30d={sc.hit_rate_30d if sc.hit_rate_30d is not None else 'n/a (Phase 5)'}"
        )

    if correlations:
        typer.echo("\n=== Correlated target pairs (same side, same token, within 5s) ===")
        for c in correlations:
            typer.echo(f"{c.target_a} <-> {c.target_b}: {c.co_occurrences} co-occurrences (out of {c.total_a}/{c.total_b} total fills)")


def _parse_since(since: str) -> dt.datetime:
    if since.endswith("d") and since[:-1].isdigit():
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(since[:-1]))
    import dateutil.parser

    return dateutil.parser.isoparse(since)


@app.command()
def control(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
) -> None:
    """Run the control plane (FR-C-*). Phase 0: stub with security defaults enforced."""
    import uvicorn

    from pmex_shadow.control.app import create_app

    settings = Settings()
    fastapi_app = create_app(settings)
    uvicorn.run(
        fastapi_app,
        host=host or settings.control_bind_host,
        port=port or settings.control_bind_port,
    )


@app.command()
def backup(
    schedule: str | None = typer.Option(None, "--schedule", help="cron expression; omit to run once"),
) -> None:
    """pg_dump the event store, record to `backups`, optionally on a cron schedule (FR-O-2)."""
    from pmex_shadow.ops.backup import run_backup_once, run_scheduled

    settings = Settings()
    if schedule:
        asyncio.run(run_scheduled(settings, schedule))
    else:
        path = asyncio.run(run_backup_once(settings))
        typer.secho(f"backup written: {path}", fg=typer.colors.GREEN)


_DEFAULT_POLICY_YAML = """\
# Conservative defaults — someone will clone this and run it without reading further.
profiles:
  tight:
    max_slippage_ticks: 1
    volatility_guard: { window_s: 5, max_ticks: 2 }
    max_fill_age_s: 5
    sizing:
      mode: target_size_percentile
      base_unit_usd: "10"
      curve: [{ p: 50, mult: "1.0" }, { p: 80, mult: "1.5" }, { p: 95, mult: "2.5" }]
      min_target_size_percentile: "60"
      min_order_usd: "5"
      max_position_usd: "50"
      max_concurrent_positions: 8
      reserve_pct: "20"
  loose:
    max_slippage_ticks: 4
    volatility_guard: { window_s: 30, max_ticks: 8 }
    max_fill_age_s: 30
    sizing:
      mode: target_size_percentile
      base_unit_usd: "10"
      curve: [{ p: 50, mult: "1.0" }, { p: 80, mult: "1.5" }, { p: 95, mult: "2.5" }]
      min_target_size_percentile: "60"
      min_order_usd: "5"
      max_position_usd: "75"
      max_concurrent_positions: 8
      reserve_pct: "20"

risk:
  global_max_exposure_usd: "5000"
  max_orders_per_minute: 30
  halt_on_reconcile_drift_usd: "100"

targets:
  decay:
    window_days: 30
    min_hit_rate: "0.45"
    auto_pause: true
  dormancy_days: 21

exits:
  mirror_sells: true
  auto_redeem: true
  redeem_retry_days: 30
"""


if __name__ == "__main__":
    app()
