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
    import_key: bool = typer.Option(False, "--import", help="use an existing wallet instead of generating one — prompts for the private key, hidden"),
    private_key_env: str | None = typer.Option(None, "--private-key-env", help="read the existing private key from this already-set env var instead of prompting (for scripted use)"),
) -> None:
    """Scaffold bots/<name>.yaml, derive a wallet + CLOB creds, print the funding address.

    By default generates a fresh, empty EOA (zero risk — nothing to lose, nothing
    funded). --import or --private-key-env instead use a wallet you already have,
    which may already hold real funds — see the confirmation prompt below."""
    from pmex_shadow.ops.wallets import provision_bot_wallet

    if import_key and private_key_env:
        typer.secho("--import and --private-key-env are mutually exclusive — pick one", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    BOTS_DIR.mkdir(exist_ok=True)
    SECRETS_DIR.mkdir(exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)

    bot_yaml_path = BOTS_DIR / f"{name}.yaml"
    if bot_yaml_path.exists():
        typer.secho(f"{bot_yaml_path} already exists — refusing to overwrite", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    imported_key: str | None = None
    if private_key_env:
        imported_key = os.environ.get(private_key_env)
        if not imported_key:
            typer.secho(f"${private_key_env} is not set or empty", fg=typer.colors.RED)
            raise typer.Exit(code=1)
    elif import_key:
        imported_key = typer.prompt("Private key to import (hex, 0x-prefixed or not)", hide_input=True)

    if imported_key:
        from eth_account import Account

        try:
            address = Account.from_key(imported_key).address
        except Exception as exc:
            typer.secho(f"not a valid private key: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc
        typer.secho(f"Importing wallet {address} — this may already hold real funds.", fg=typer.colors.YELLOW, bold=True)
        if not typer.confirm(f"Store this private key in {SECRETS_DIR / f'{name}.env'} on this machine and use it for '{name}'?"):
            typer.echo("aborted")
            raise typer.Exit(code=1)

    typer.echo(f"Deriving wallet and CLOB credentials for '{name}' (live network call)...")
    try:
        wallet = asyncio.run(provision_bot_wallet(private_key=imported_key))
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
    if imported_key:
        typer.secho(f"imported wallet: {wallet.funding_address}", fg=typer.colors.CYAN, bold=True)
        typer.echo("Run `pmex-shadow doctor --bot <name>` to confirm balances/allowances on this address before running live.")
    else:
        typer.secho(f"FUND THIS ADDRESS: {wallet.funding_address}", fg=typer.colors.CYAN, bold=True)
    typer.echo("Edit bots/<name>.yaml to set targets/selectors, then `pmex-shadow doctor --bot <name>`.")


@bot_app.command("run")
def bot_run(name: str, live: bool = typer.Option(False, "--live")) -> None:
    """Run a bot: consume fills, decide, and (paper-simulate or, in live mode,
    actually submit) orders. Also supervises the shared watcher's heartbeat and
    halts if it goes stale (FR-EXE-8).

    Live-mode interlock (FR-O-5) — three independent conditions, checked here in one
    place so no code path can accidentally trade live by skipping it:
    `mode: live` in bots/<name>.yaml, the `--live` flag, and a non-empty
    I_UNDERSTAND_THIS_TRADES_REAL_FUNDS. Any missing -> refuse to start, naming which.
    """
    import os

    import asyncpg

    from pmex_shadow.config import load_policy_file
    from pmex_shadow.execution.clob import ClobClient
    from pmex_shadow.execution.consumer import BotConsumer
    from pmex_shadow.execution.deadman import DeadmanSwitch
    from pmex_shadow.execution.ratelimit import TokenBucket
    from pmex_shadow.execution.router import ExecutionRouter

    bot_yaml_path = BOTS_DIR / f"{name}.yaml"
    if not bot_yaml_path.exists():
        typer.secho(f"{bot_yaml_path} not found — run `pmex-shadow bot new {name}` first", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    cfg = load_bot_config(bot_yaml_path)
    settings = Settings()
    policy_file = load_policy_file(POLICY_FILE)

    if live:
        missing = []
        if cfg.mode != "live":
            missing.append(f"bots/{name}.yaml has mode: {cfg.mode!r}, not mode: live")
        if not settings.i_understand_this_trades_real_funds:
            missing.append("I_UNDERSTAND_THIS_TRADES_REAL_FUNDS is not set")
        if missing:
            typer.secho("Refusing to start in live mode — missing:", fg=typer.colors.RED)
            for m in missing:
                typer.secho(f"  - {m}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
    elif cfg.mode == "live":
        typer.secho(f"bots/{name}.yaml has mode: live but --live was not passed — refusing to start (would silently run a live-configured bot in a lesser mode)", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    mode = cfg.mode

    async def _run() -> None:
        conn = await asyncpg.connect(settings.database_url)
        clob = None
        deadman_task = None
        reconcile_task = None
        redeem_task = None
        paper_resolution_task = None
        try:
            from pmex_shadow.control.config_write import seed_initial_config
            from pmex_shadow.watcher.heartbeat import run_heartbeat_loop

            await seed_initial_config(conn, cfg)  # FR-C-3 rule 1: YAML seeds the DB row once; DB is authoritative after
            bot_heartbeat_task = asyncio.create_task(run_heartbeat_loop(settings.database_url, f"bot:{name}", lambda: {"mode": mode}))

            rate_limiter = TokenBucket(rate_per_minute=policy_file.risk.max_orders_per_minute)

            if mode == "live":
                pk = os.environ.get(cfg.wallet.pk_env)
                funder = os.environ.get(cfg.wallet.funder_env)
                api_key = os.environ.get(f"{name.upper()}_API_KEY")
                api_secret = os.environ.get(f"{name.upper()}_API_SECRET")
                api_passphrase = os.environ.get(f"{name.upper()}_API_PASSPHRASE")
                if not all([pk, funder, api_key, api_secret, api_passphrase]):
                    typer.secho(f"live mode: missing credentials in secrets/{name}.env (wallet or API key/secret/passphrase)", fg=typer.colors.RED)
                    raise typer.Exit(code=1)
                clob = await ClobClient.create(private_key=pk, wallet=funder, api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)

                deadman = DeadmanSwitch(
                    clob_base_url=settings.clob_base_url, bot_id=name, api_key=api_key, api_secret=api_secret,
                    api_passphrase=api_passphrase, address=funder, timeout_s=settings.deadman_timeout_s,
                )

                async def _on_trip():
                    await clob.cancel_all()

                deadman_task = asyncio.create_task(deadman.run(settings.database_url, _on_trip))

                from pmex_shadow.ledger.reconcile import run_reconcile_loop
                from pmex_shadow.ledger.redeem import run_redeem_loop

                reconcile_task = asyncio.create_task(run_reconcile_loop(
                    settings.database_url, name, funder, mode, policy_file.risk.halt_on_reconcile_drift_usd, interval_s=60,
                ))
                if not settings.polygon_rpc_url:
                    typer.secho("POLYGON_RPC_URL not set — redemption's POL-balance safety check cannot run; redeem loop disabled", fg=typer.colors.YELLOW)
                else:
                    redeem_task = asyncio.create_task(run_redeem_loop(
                        settings.database_url, name, funder, mode, settings.polygon_rpc_url,
                        policy_file.exits.redeem_retry_days, clob.sdk_client, interval_s=3600,
                    ))
            elif mode == "paper":
                from pmex_shadow.ledger.redeem import run_paper_resolution_loop

                # FR-EXE-10: paper positions have no real on-chain holdings to check
                # resolution against (never touch a real wallet), so this uses Gamma's
                # closed/outcomePrices view instead of redeem_task's on-chain check —
                # without this a resolved market's position just sits open forever,
                # eventually blocking every new trade on max_concurrent_positions.
                paper_resolution_task = asyncio.create_task(run_paper_resolution_loop(
                    settings.database_url, name, settings.gamma_api_base_url, interval_s=60,
                ))

            router = ExecutionRouter(
                bot_id=name, mode=mode, database_url=settings.database_url, clob_base_url=settings.clob_base_url,
                clob=clob, rate_limiter=rate_limiter, submit_timeout_s=10,
            )
            consumer = BotConsumer(
                bot=cfg, policy_file=policy_file, database_url=settings.database_url,
                gamma_api_base_url=settings.gamma_api_base_url, clob_base_url=settings.clob_base_url, router=router,
            )

            router_task = asyncio.create_task(router.run())
            consumer_task = asyncio.create_task(consumer.run())

            stop_event = asyncio.Event()

            def _handle_sigterm() -> None:
                stop_event.set()

            loop = asyncio.get_running_loop()
            import signal

            loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

            typer.echo(f"bot '{name}' running (mode={mode}, live_interlock={'satisfied' if live else 'n/a'})")

            heartbeat_task = asyncio.create_task(_supervise_watcher_heartbeat(conn, name, settings.watcher_stale_s, stop_event))

            await stop_event.wait()

            typer.echo(f"bot '{name}' stopping...")
            await router.stop()
            for t in (router_task, consumer_task, heartbeat_task, bot_heartbeat_task):
                t.cancel()
            for t in (deadman_task, reconcile_task, redeem_task, paper_resolution_task):
                if t:
                    t.cancel()
        finally:
            if clob is not None:
                await clob.close()
            await conn.close()

    asyncio.run(_run())


async def _supervise_watcher_heartbeat(conn, bot_id: str, watcher_stale_s: int, stop_event: "asyncio.Event") -> None:
    from pmex_shadow.ops.health import heartbeat_age

    while True:
        age = await heartbeat_age(conn, "watcher")
        if age is None or age.total_seconds() > watcher_stale_s:
            detail = "never reported" if age is None else f"{age.total_seconds():.1f}s old (limit {watcher_stale_s}s)"
            await conn.execute(
                "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'CRITICAL', 'bot.health', 'watcher heartbeat stale, halting', $2)",
                bot_id, f'{{"detail": "{detail}"}}',
            )
            typer.secho(f"HALTED: watcher heartbeat {detail}", fg=typer.colors.RED)
            stop_event.set()
            return
        await asyncio.sleep(5)


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
    """Register a target wallet address for the watcher to track (target_stats).
    Active immediately — no probation period."""
    import asyncpg

    from pmex_shadow.targets.registry import InvalidAddress, register_target

    settings = Settings()

    async def _add() -> None:
        conn = await asyncpg.connect(settings.database_url)
        try:
            return await register_target(conn, address, alias)
        finally:
            await conn.close()

    try:
        asyncio.run(_add())
    except InvalidAddress as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho(f"tracking {address.lower()}" + (f" (alias: {alias})" if alias else ""), fg=typer.colors.GREEN)
    typer.echo("status: active. The watcher picks it up automatically (Data API sweep within one poll cycle; chain/WS path on its next reconnect).")


@targets_app.command("list")
def targets_list() -> None:
    """List currently tracked targets."""
    import asyncpg

    settings = Settings()

    async def _list():
        conn = await asyncpg.connect(settings.database_url)
        try:
            return await conn.fetch(
                "SELECT target, alias, status, last_fill_at, hit_rate_30d, fills_30d, reversal_rate FROM target_stats ORDER BY target"
            )
        finally:
            await conn.close()

    rows = asyncio.run(_list())
    if not rows:
        typer.echo("no targets tracked yet — `pmex-shadow targets add <address>`")
        return
    for r in rows:
        typer.echo(
            f"{r['target']}  alias={r['alias'] or '-'}  status={r['status']}  last_fill={r['last_fill_at'] or 'never'}  "
            f"hit_rate_30d={r['hit_rate_30d'] if r['hit_rate_30d'] is not None else 'n/a'}  fills_30d={r['fills_30d'] or 0}  "
            f"reversal_rate={r['reversal_rate'] if r['reversal_rate'] is not None else 'n/a'}"
        )


@targets_app.command("pause")
def targets_pause(address: str) -> None:
    """Manually pause a target (status=paused_manual). Never auto-un-paused."""
    import asyncpg

    settings = Settings()

    async def _pause() -> None:
        conn = await asyncpg.connect(settings.database_url)
        try:
            result = await conn.execute("UPDATE target_stats SET status = 'paused_manual' WHERE target = $1", address.lower())
            return result
        finally:
            await conn.close()

    result = asyncio.run(_pause())
    if result == "UPDATE 0":
        typer.secho(f"{address.lower()} is not tracked", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho(f"paused {address.lower()}", fg=typer.colors.GREEN)


@targets_app.command("resume")
def targets_resume(address: str) -> None:
    """Resume a paused target (status=active). Explicit operator action only —
    nothing in this system auto-resumes a paused target."""
    import asyncpg

    settings = Settings()

    async def _resume() -> None:
        conn = await asyncpg.connect(settings.database_url)
        try:
            return await conn.execute("UPDATE target_stats SET status = 'active' WHERE target = $1", address.lower())
        finally:
            await conn.close()

    result = asyncio.run(_resume())
    if result == "UPDATE 0":
        typer.secho(f"{address.lower()} is not tracked", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho(f"resumed {address.lower()}", fg=typer.colors.GREEN)


@targets_app.command("migrate")
def targets_migrate(old_address: str, new_address: str) -> None:
    """Reassign a target's history to a new proxy address (FR-T-6) — a target
    changing wallets, not forking their track record."""
    import asyncpg

    settings = Settings()
    old_addr, new_addr = old_address.lower(), new_address.lower()

    async def _migrate() -> None:
        conn = await asyncpg.connect(settings.database_url)
        try:
            async with conn.transaction():
                old_row = await conn.fetchrow("SELECT * FROM target_stats WHERE target = $1", old_addr)
                if old_row is None:
                    raise typer.Exit(code=1)
                await conn.execute("UPDATE target_fills SET target = $2 WHERE target = $1", old_addr, new_addr)
                await conn.execute(
                    """
                    INSERT INTO target_stats (target, alias, size_p50, size_p60, size_p80, size_p95,
                        fills_30d, hit_rate_30d, pnl_30d_usd, reversal_rate, last_fill_at, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (target) DO NOTHING
                    """,
                    new_addr, old_row["alias"], old_row["size_p50"], old_row["size_p60"], old_row["size_p80"],
                    old_row["size_p95"], old_row["fills_30d"], old_row["hit_rate_30d"], old_row["pnl_30d_usd"],
                    old_row["reversal_rate"], old_row["last_fill_at"], old_row["status"],
                )
                await conn.execute("DELETE FROM target_stats WHERE target = $1", old_addr)
                await conn.execute(
                    "INSERT INTO events (level, component, message, context) VALUES ('WARN', 'targets.migrate', 'target migrated', $1)",
                    f'{{"old": "{old_addr}", "new": "{new_addr}"}}',
                )
        finally:
            await conn.close()

    try:
        asyncio.run(_migrate())
    except typer.Exit:
        typer.secho(f"{old_addr} is not tracked", fg=typer.colors.RED)
        raise
    typer.secho(f"migrated {old_addr} -> {new_addr} (history preserved)", fg=typer.colors.GREEN)
    typer.echo("Update bots/*.yaml targets: lists that reference the old address or alias.")


@targets_app.command("recompute")
def targets_recompute(
    schedule: str | None = typer.Option(None, "--schedule", help="cron expression; omit to run once"),
) -> None:
    """Recompute target_stats (FR-T-1) and apply decay/dormancy auto-pause (FR-T-2,
    FR-T-3)."""
    import asyncpg

    from pmex_shadow.config import load_policy_file
    from pmex_shadow.targets.decay import DecayCheckInput, check_decay
    from pmex_shadow.targets.stats import recompute_all_targets

    settings = Settings()
    policy_file = load_policy_file(POLICY_FILE)

    async def _pass() -> None:
        conn = await asyncpg.connect(settings.database_url)
        try:
            n = await recompute_all_targets(conn)
            rows = await conn.fetch("SELECT * FROM target_stats")
            now = dt.datetime.now(dt.timezone.utc)
            for row in rows:
                new_status = check_decay(DecayCheckInput(
                    status=row["status"], hit_rate_30d=row["hit_rate_30d"], fills_30d=row["fills_30d"] or 0,
                    last_fill_at=row["last_fill_at"], now=now,
                    min_hit_rate=policy_file.targets.decay.min_hit_rate,
                    min_sample_size=10, dormancy_days=policy_file.targets.dormancy_days,
                ))
                if new_status:
                    await conn.execute("UPDATE target_stats SET status = $2 WHERE target = $1", row["target"], new_status)
                    await conn.execute(
                        "INSERT INTO events (level, component, message, context) VALUES ('WARN', 'targets.decay', 'auto-paused', $1)",
                        f'{{"target": "{row["target"]}", "new_status": "{new_status}"}}',
                    )
            return n
        finally:
            await conn.close()

    if schedule:
        from croniter import croniter

        async def _loop():
            itr = croniter(schedule, dt.datetime.now(dt.timezone.utc))
            while True:
                next_run = itr.get_next(dt.datetime)
                sleep_s = (next_run - dt.datetime.now(dt.timezone.utc)).total_seconds()
                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)
                try:
                    n = await _pass()
                    typer.echo(f"recomputed {n} target(s)")
                except Exception:
                    logging.getLogger("pmex_shadow.targets").exception("recompute pass failed")

        asyncio.run(_loop())
    else:
        n = asyncio.run(_pass())
        typer.secho(f"recomputed {n} target(s)", fg=typer.colors.GREEN)


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


@bot_app.command("overlap")
def bots_overlap() -> None:
    """FR-O-7: report bot pairs sharing targets with intersecting selectors, and
    their combined exposure. Not checked automatically at `bot new` time — a fresh
    scaffold always has an empty targets: [] until the operator edits the YAML, so
    there's nothing to overlap with yet; run this after configuring targets instead.
    """
    from pmex_shadow.ops.compose_gen import compute_overlaps, load_all_bots

    bots = load_all_bots(BOTS_DIR)
    overlaps = compute_overlaps(bots)
    if not overlaps:
        typer.echo("no overlaps detected")
        return
    for o in overlaps:
        typer.secho(
            f"{o['bot_a']} overlaps {o['bot_b']} on {len(o['shared_targets'])} target(s) "
            f"({', '.join(o['shared_targets'])}) — combined exposure ${o['combined_exposure_usd']}",
            fg=typer.colors.YELLOW,
        )


@bot_app.command("resume")
def bot_resume(name: str) -> None:
    """Clear a halt (reconcile drift or killswitch) — explicit operator action only,
    nothing in this system auto-resumes."""
    import asyncpg

    from pmex_shadow.ops.killswitch import resume_bot

    settings = Settings()

    async def _resume():
        conn = await asyncpg.connect(settings.database_url)
        try:
            await resume_bot(conn, name)
        finally:
            await conn.close()

    asyncio.run(_resume())
    typer.secho(f"resumed {name}", fg=typer.colors.GREEN)


@bot_app.command("archive")
def bot_archive(
    name: str,
    reason: str | None = typer.Option(None, "--reason", help="why it was retired — shown in `bot list`"),
    force: bool = typer.Option(False, "--force", help="archive despite blockers (running, or open positions)"),
) -> None:
    """Retire a bot from the fleet view without deleting anything.

    History stays queryable and the bot stays selectable in the Logs/Analysis
    filters — this only stops it being presented as something you operate. It is
    not a halt (see `panic`/`bot resume`) and not a delete; `bot unarchive`
    reverses it. Stop the container separately: nothing here kills a process.
    """
    import asyncpg

    from pmex_shadow.ops.registry import archive_blockers, archive_bot

    settings = Settings()

    async def _archive():
        conn = await asyncpg.connect(settings.database_url)
        try:
            blockers = await archive_blockers(conn, name)
            if blockers and not force:
                return None, blockers
            return await archive_bot(conn, name, reason), blockers
        finally:
            await conn.close()

    archived, blockers = asyncio.run(_archive())

    if archived is None:
        typer.secho(f"refusing to archive {name}:", fg=typer.colors.RED)
        for b in blockers:
            typer.echo(f"  - {b}")
        typer.echo("Archiving is a presentation change, not a stop — halt or drain it first, or pass --force.")
        raise typer.Exit(code=1)
    if not archived:
        typer.secho(f"unknown bot '{name}' — not in the bots registry", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    for b in blockers:
        typer.secho(f"  forced past: {b}", fg=typer.colors.YELLOW)
    typer.secho(f"archived {name}", fg=typer.colors.GREEN)
    typer.echo("Its history is unchanged and still reachable at /bots/" + name + ".")


@bot_app.command("unarchive")
def bot_unarchive(name: str) -> None:
    """Return an archived bot to the fleet view. Does not start it."""
    import asyncpg

    from pmex_shadow.ops.registry import unarchive_bot

    settings = Settings()

    async def _unarchive():
        conn = await asyncpg.connect(settings.database_url)
        try:
            return await unarchive_bot(conn, name)
        finally:
            await conn.close()

    if not asyncio.run(_unarchive()):
        typer.secho(f"unknown bot '{name}' — not in the bots registry", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho(f"unarchived {name} — still stopped; `bot run {name}` to start it", fg=typer.colors.GREEN)


@bot_app.command("list")
def bot_list() -> None:
    """Every registered bot, archived included — unlike the fleet view."""
    import asyncpg

    from pmex_shadow.ops.registry import list_bots

    settings = Settings()

    async def _list():
        conn = await asyncpg.connect(settings.database_url)
        try:
            return await list_bots(conn)
        finally:
            await conn.close()

    bots = asyncio.run(_list())
    if not bots:
        typer.echo("no bots registered")
        return
    for b in bots:
        if b["archived_at"]:
            detail = f"archived {b['archived_at']:%Y-%m-%d}"
            if b["archived_reason"]:
                detail += f" — {b['archived_reason']}"
            typer.secho(f"  {b['bot_id']:24} {detail}", fg=typer.colors.BRIGHT_BLACK)
        else:
            typer.echo(f"  {b['bot_id']:24} active")


@app.command()
def panic(
    bot: str | None = typer.Option(None, "--bot", help="halt only this bot; omit for all"),
    flatten: bool = typer.Option(False, "--flatten", help="also cancel resting orders (does NOT auto-liquidate positions)"),
    reason: str = typer.Option("manual panic", "--reason"),
) -> None:
    """Halt every bot, or one (FR-O-3). Halting stops new order generation
    immediately (decide() checks ledger.halted on every fill) — resuming is always
    a separate, explicit `bot resume`."""
    import asyncpg

    from pmex_shadow.ops.killswitch import cancel_resting_orders, halt_all, halt_bot

    settings = Settings()

    async def _panic():
        conn = await asyncpg.connect(settings.database_url)
        try:
            if bot:
                await halt_bot(conn, bot, reason, flatten)
                halted = [bot]
            else:
                halted = await halt_all(conn, reason, flatten)
        finally:
            await conn.close()
        return halted

    halted = asyncio.run(_panic())
    typer.secho(f"halted: {', '.join(halted)}", fg=typer.colors.RED)

    if flatten:
        for bot_id in halted:
            bot_yaml_path = BOTS_DIR / f"{bot_id}.yaml"
            if not bot_yaml_path.exists():
                continue
            cfg = load_bot_config(bot_yaml_path)
            ok = asyncio.run(cancel_resting_orders(
                bot_id, cfg.wallet.funder_env, cfg.wallet.pk_env, bot_id.upper(), settings.clob_base_url,
            ))
            if ok:
                typer.secho(f"  {bot_id}: resting orders cancelled", fg=typer.colors.YELLOW)
            else:
                typer.secho(f"  {bot_id}: no live credentials in environment — nothing to cancel (paper mode, or run this from the bot's own context)", fg=typer.colors.YELLOW)
        typer.echo("Open positions were NOT auto-liquidated — flatten cancels resting orders only. Review and close positions manually if intended.")


@app.command()
def export(
    kind: str = typer.Option("fills", "--kind", help="fills or pnl"),
    since: str = typer.Option("2026-01-01", "--since"),
    output: Path = typer.Option(Path("export.csv"), "--output"),
) -> None:
    """CSV export of fills or realized PnL — for taxes (design doc §3.7)."""
    import asyncpg
    import dateutil.parser

    from pmex_shadow.research.export import export_fills_csv, export_pnl_csv

    settings = Settings()
    since_dt = dateutil.parser.isoparse(since)
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=dt.timezone.utc)

    async def _export():
        conn = await asyncpg.connect(settings.database_url)
        try:
            if kind == "fills":
                return await export_fills_csv(conn, since_dt)
            elif kind == "pnl":
                return await export_pnl_csv(conn, since_dt)
            else:
                typer.secho(f"unknown --kind {kind!r}, expected fills or pnl", fg=typer.colors.RED)
                raise typer.Exit(code=1)
        finally:
            await conn.close()

    csv_text = asyncio.run(_export())
    output.write_text(csv_text)
    typer.secho(f"wrote {output}", fg=typer.colors.GREEN)


compose_app = typer.Typer(help="Generate compose files from bots/*.yaml")
app.add_typer(compose_app, name="compose")


@compose_app.command("generate")
def compose_generate() -> None:
    """bots/*.yaml -> docker-compose.bots.yml. Adding a bot is dropping a YAML and
    regenerating — never hand-edit the output."""
    from pmex_shadow.ops.compose_gen import generate_compose, load_all_bots

    bots = load_all_bots(BOTS_DIR)
    if not bots:
        typer.echo("no bots configured yet — nothing to generate")
        return
    output = generate_compose(bots)
    Path("docker-compose.bots.yml").write_text(output)
    typer.secho(f"wrote docker-compose.bots.yml ({len(bots)} bot service(s))", fg=typer.colors.GREEN)


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
