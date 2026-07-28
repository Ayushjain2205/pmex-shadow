"""`pmex-shadow backup` — FR-O-2. The event store is the cost basis; back it up or lose
what you own and what you paid for it (design doc §3.9)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
from croniter import croniter

from pmex_shadow.config import Settings

logger = logging.getLogger("pmex_shadow.backup")


def _pg_dump_args(database_url: str) -> tuple[list[str], dict[str, str]]:
    parsed = urlparse(database_url)
    env: dict[str, str] = {}
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    args = [
        "pg_dump",
        "-h", parsed.hostname or "localhost",
        "-p", str(parsed.port or 5432),
        "-U", parsed.username or "postgres",
        "-d", (parsed.path or "/pmex").lstrip("/"),
        "-Fc",
    ]
    return args, env


async def run_backup_once(settings: Settings) -> Path:
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = backup_dir / f"pmex-{timestamp}.dump"

    args, extra_env = _pg_dump_args(settings.database_url)
    import os

    env = {**os.environ, **extra_env}
    succeeded = False
    size_bytes = 0
    try:
        with out_path.open("wb") as f:
            proc = subprocess.run(args, stdout=f, stderr=subprocess.PIPE, env=env, timeout=600)
        if proc.returncode != 0:
            logger.error("pg_dump failed: %s", proc.stderr.decode(errors="replace"))
        else:
            succeeded = True
            size_bytes = out_path.stat().st_size
    except Exception:
        logger.exception("backup failed")

    conn = await asyncpg.connect(settings.database_url)
    try:
        await conn.execute(
            "INSERT INTO backups (path, bytes, succeeded) VALUES ($1, $2, $3)",
            str(out_path), size_bytes, succeeded,
        )
    finally:
        await conn.close()

    if not succeeded:
        raise RuntimeError(f"backup failed — see logs; recorded to backups table for doctor to see")
    logger.info("backup succeeded: %s (%d bytes)", out_path, size_bytes)
    return out_path


async def run_scheduled(settings: Settings, cron_expr: str) -> None:
    base = dt.datetime.now(dt.timezone.utc)
    itr = croniter(cron_expr, base)
    logger.info("backup service started, schedule=%r", cron_expr)
    while True:
        next_run = itr.get_next(dt.datetime)
        sleep_s = (next_run - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        try:
            await run_backup_once(settings)
        except Exception:
            logger.exception("scheduled backup failed")
