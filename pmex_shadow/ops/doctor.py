"""`pmex-shadow doctor` — FR-O-1. Every check is PASS / WARN / FAIL with remediation
text. Only a FAIL causes a non-zero exit; WARN covers expected empty states on a fresh
clone (no bots configured yet, no backups yet) so `doctor` stays useful before anything
has been set up, per the Definition of Done in PRD §11.

Contract addresses below are pinned from docs/VERIFIED.md (verified 2026-07-29) —
do not "helpfully" hardcode different ones without re-verifying and updating that file.
"""

from __future__ import annotations

import shutil
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from pmex_shadow.config import BotConfig, Settings, load_bot_config
from pmex_shadow.contracts import CTF_EXCHANGE_V2, PUSD_COLLATERAL

Status = str  # "PASS" | "WARN" | "FAIL"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    remediation: str = ""


def _discover_bots(bots_dir: Path) -> list[BotConfig]:
    bots: list[BotConfig] = []
    if not bots_dir.exists():
        return bots
    for path in sorted(bots_dir.glob("*.yaml")):
        try:
            bots.append(load_bot_config(path))
        except Exception:
            continue
    return bots


async def check_disk_free(min_free_gb: float = 1.0, warn_free_gb: float = 5.0) -> CheckResult:
    usage = shutil.disk_usage("/")
    free_gb = usage.free / (1024**3)
    if free_gb < min_free_gb:
        return CheckResult(
            "disk_free", "FAIL", f"{free_gb:.2f} GB free",
            f"free at least {min_free_gb} GB — an out-of-space Postgres is how you lose the event store",
        )
    if free_gb < warn_free_gb:
        return CheckResult("disk_free", "WARN", f"{free_gb:.2f} GB free", "consider freeing disk soon")
    return CheckResult("disk_free", "PASS", f"{free_gb:.2f} GB free")


async def check_clock_skew(max_skew_s: float = 2.0) -> CheckResult:
    """SNTP query against pool.ntp.org — clock skew breaks timestamped CLOB auth headers."""
    NTP_EPOCH_OFFSET = 2208988800
    packet = b"\x1b" + 47 * b"\0"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(5)
            sock.sendto(packet, ("pool.ntp.org", 123))
            local_send = time.time()
            data, _ = sock.recvfrom(48)
            local_recv = time.time()
        unpacked = struct.unpack("!12I", data)
        ntp_time = unpacked[10] - NTP_EPOCH_OFFSET
        local_mid = (local_send + local_recv) / 2
        skew = local_mid - ntp_time
    except OSError as exc:
        return CheckResult("clock_skew", "WARN", f"NTP query failed: {exc}", "cannot verify clock skew — check outbound UDP/123")

    if abs(skew) > max_skew_s:
        return CheckResult(
            "clock_skew", "FAIL", f"{skew:+.2f}s vs pool.ntp.org",
            "sync the host clock (chrony/ntpd) — CLOB auth headers are timestamp-signed and will be rejected",
        )
    return CheckResult("clock_skew", "PASS", f"{skew:+.2f}s vs pool.ntp.org")


async def check_clob_reachability(settings: Settings) -> CheckResult:
    url = settings.clob_base_url.rstrip("/") + "/"
    try:
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        rtt_ms = (time.monotonic() - start) * 1000
    except httpx.HTTPError as exc:
        return CheckResult("clob_reachability", "FAIL", f"{url}: {exc}", "check network egress / CLOB_BASE_URL")

    if resp.status_code != 200:
        return CheckResult("clob_reachability", "FAIL", f"HTTP {resp.status_code}", "CLOB API returned an error status")
    return CheckResult("clob_reachability", "PASS", f"HTTP {resp.status_code}, {rtt_ms:.0f}ms RTT")


async def check_rpc_and_contracts(settings: Settings) -> list[CheckResult]:
    if not settings.polygon_rpc_url:
        return [
            CheckResult(
                "rpc_reachability", "WARN", "POLYGON_RPC_URL not set",
                "chain source is optional (FR-W-7) — set it to enable on-chain checks and the chain watcher",
            ),
            CheckResult("exchange_contract_abi", "WARN", "skipped — no RPC configured", "set POLYGON_RPC_URL"),
        ]

    results: list[CheckResult] = []
    w3 = AsyncWeb3(AsyncHTTPProvider(settings.polygon_rpc_url))

    samples: list[float] = []
    try:
        for _ in range(3):
            start = time.monotonic()
            await w3.eth.block_number
            samples.append((time.monotonic() - start) * 1000)
    except Exception as exc:
        results.append(CheckResult("rpc_reachability", "FAIL", f"{exc}", "check POLYGON_RPC_URL / provider status"))
        return results

    samples.sort()
    p50 = samples[len(samples) // 2]
    p99 = samples[-1]
    results.append(CheckResult("rpc_reachability", "PASS", f"p50={p50:.0f}ms p99={p99:.0f}ms (n={len(samples)})"))

    try:
        code = await w3.eth.get_code(w3.to_checksum_address(CTF_EXCHANGE_V2))
        if len(code) == 0:
            results.append(CheckResult("exchange_contract_abi", "FAIL", "no bytecode at CTF Exchange V2 address", "verify docs/VERIFIED.md item 1 against docs.polymarket.com/resources/contracts"))
        else:
            selector = w3.keccak(text="getCollateral()")[:4]
            raw = await w3.eth.call({"to": w3.to_checksum_address(CTF_EXCHANGE_V2), "data": selector})
            returned_addr = "0x" + raw[-20:].hex()
            if returned_addr.lower() == PUSD_COLLATERAL.lower():
                results.append(CheckResult("exchange_contract_abi", "PASS", "getCollateral() == pUSD as expected"))
            else:
                results.append(CheckResult(
                    "exchange_contract_abi", "FAIL", f"getCollateral() returned {returned_addr}, expected {PUSD_COLLATERAL}",
                    "contract has changed since docs/VERIFIED.md was written — re-run §9 verification",
                ))
    except Exception as exc:
        results.append(CheckResult("exchange_contract_abi", "FAIL", f"{exc}", "RPC call failed — check provider"))

    return results


async def check_bot_wallets(settings: Settings, bots: list[BotConfig]) -> list[CheckResult]:
    if not bots:
        return [CheckResult("bot_wallets", "WARN", "no bots configured", "run `pmex-shadow bot new <name>`")]
    if not settings.polygon_rpc_url:
        return [CheckResult("bot_wallets", "WARN", "POLYGON_RPC_URL not set", "cannot check pUSD/POL balances without RPC")]

    results: list[CheckResult] = []
    w3 = AsyncWeb3(AsyncHTTPProvider(settings.polygon_rpc_url))
    import os

    for bot in bots:
        funder_addr = os.environ.get(f"{bot.wallet.funder_env}_ADDRESS") or os.environ.get(bot.wallet.funder_env)
        if not funder_addr:
            results.append(CheckResult(
                f"bot_wallet[{bot.name}]", "WARN", f"${bot.wallet.funder_env} not set in environment",
                "fund and configure the bot wallet — see `bot new`",
            ))
            continue
        try:
            addr = w3.to_checksum_address(funder_addr)
            pol_balance = await w3.eth.get_balance(addr)
            pol = pol_balance / 10**18
            status = "PASS" if pol > 0 else "WARN"
            results.append(CheckResult(f"bot_wallet[{bot.name}]_pol", status, f"{pol:.4f} POL", "fund the wallet with a small POL float for gas" if status == "WARN" else ""))
        except Exception as exc:
            results.append(CheckResult(f"bot_wallet[{bot.name}]", "FAIL", f"{exc}", "check wallet address / RPC"))

    return results


async def check_targets_resolved(bots: list[BotConfig]) -> CheckResult:
    all_targets = {t for bot in bots for t in bot.targets}
    if not all_targets:
        return CheckResult("target_wallets_resolved", "WARN", "no targets configured", "run `pmex-shadow targets add`")
    return CheckResult("target_wallets_resolved", "WARN", f"{len(all_targets)} target(s) referenced", "target→proxy-wallet resolution ships in Phase 1")


async def check_alembic_head(database_url: str) -> CheckResult:
    # alembic.ini ships next to the package's install root (repo root in dev, /app in
    # the container — both are the process's cwd per the Dockerfile WORKDIR).
    import os

    ini_path = Path(os.environ.get("PMEX_ALEMBIC_INI", Path.cwd() / "alembic.ini"))
    if not ini_path.exists():
        return CheckResult(
            "alembic_head", "FAIL", f"alembic.ini not found at {ini_path}",
            "run doctor from the repo root, or set PMEX_ALEMBIC_INI",
        )
    alembic_cfg = AlembicConfig(str(ini_path))
    script = ScriptDirectory.from_config(alembic_cfg)
    head = script.get_current_head()

    try:
        conn = await asyncpg.connect(database_url)
    except Exception as exc:
        return CheckResult("alembic_head", "FAIL", f"cannot connect to database: {exc}", "check PMEX_DATABASE_URL / db container")

    try:
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'alembic_version')"
        )
        if not exists:
            return CheckResult("alembic_head", "FAIL", "alembic_version table missing", "run `alembic upgrade head`")
        current = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()

    if current != head:
        return CheckResult("alembic_head", "FAIL", f"db at {current}, repo head is {head}", "run `alembic upgrade head`")
    return CheckResult("alembic_head", "PASS", f"at head {head}")


async def check_last_backup(database_url: str, max_age_hours: int) -> CheckResult:
    try:
        conn = await asyncpg.connect(database_url)
    except Exception as exc:
        return CheckResult("last_backup_age", "FAIL", f"cannot connect to database: {exc}", "check PMEX_DATABASE_URL")

    try:
        row = await conn.fetchrow(
            "SELECT at, succeeded FROM backups WHERE succeeded ORDER BY at DESC LIMIT 1"
        )
    finally:
        await conn.close()

    if row is None:
        return CheckResult("last_backup_age", "WARN", "no successful backup recorded yet", "the backup service runs on a schedule — this clears after its first run")

    import datetime as dt

    age = dt.datetime.now(dt.timezone.utc) - row["at"]
    age_h = age.total_seconds() / 3600
    if age_h > max_age_hours:
        return CheckResult("last_backup_age", "FAIL", f"{age_h:.1f}h old (max {max_age_hours}h)", "check the backup service — event store is your cost basis")
    return CheckResult("last_backup_age", "PASS", f"{age_h:.1f}h old")


async def check_api_creds(bots: list[BotConfig]) -> CheckResult:
    if not bots:
        return CheckResult("api_cred_validity", "WARN", "no bots configured", "CLOB API creds are derived per-bot by `bot new`")
    return CheckResult("api_cred_validity", "WARN", "cred validation ships with the execution router (Phase 3)", "")


async def run_all_checks(settings: Settings, bots_dir: Path) -> list[CheckResult]:
    bots = _discover_bots(bots_dir)
    results: list[CheckResult] = []
    results.append(await check_disk_free())
    results.append(await check_clock_skew())
    results.append(await check_clob_reachability(settings))
    results.extend(await check_rpc_and_contracts(settings))
    results.extend(await check_bot_wallets(settings, bots))
    results.append(await check_targets_resolved(bots))
    results.append(await check_alembic_head(settings.database_url))
    results.append(await check_last_backup(settings.database_url, settings.backup_max_age_hours))
    results.append(await check_api_creds(bots))
    return results
