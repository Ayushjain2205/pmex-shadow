from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import typer
import yaml

from pmex_shadow.config import Settings, load_bot_config

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


@app.command()
def watcher() -> None:
    """Shared fill stream (Phase 0: heartbeat-only stub; chain/sweep ship in Phase 1)."""
    from pmex_shadow.watcher.heartbeat import run_heartbeat_loop

    settings = Settings()
    typer.echo("watcher stub starting (Phase 1 adds chain subscription + Data API sweep)")
    asyncio.run(run_heartbeat_loop(settings.database_url, "watcher", lambda: {"phase": 0}))


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
