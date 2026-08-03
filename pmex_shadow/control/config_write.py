"""Versioned bot config: validate, write, audit (FR-C-3, FR-C-4, FR-C-6). §3.8's four
rules, enforced here specifically:
  1. bots/*.yaml seeds the initial row; the DB is authoritative thereafter.
  2. Validate before apply; fail to last-good — a rejected config never leaves a bot
     unconfigured, and the previous active version stays active.
  3. Not everything is hot-reloadable: wallet requires a restart. `targets` used to
     be grouped in with wallet/name under FR-C-5 as "identity" fields, but unlike
     wallet (bound to a live signing client for the process lifetime — cli.py's
     ClobClient/ExecutionRouter construction) a bot's target set is just an
     in-process address filter (execution/consumer.py's `_target_addresses`)
     recomputed from target_stats — no in-flight order or signing state depends on
     it, so it hot-reloads like selectors/mode/policy do.
  4. Every change is audited: actor, diff, outcome.
"""

from __future__ import annotations

import json

import asyncpg
from pydantic import ValidationError

from pmex_shadow.config import BotConfig

# Fields that change the bot's identity or funding — never hot-reloadable.
NON_HOT_RELOADABLE_FIELDS = frozenset({"name", "wallet"})


async def get_active_config(conn: asyncpg.Connection, bot_id: str) -> dict | None:
    row = await conn.fetchrow("SELECT version, config, active FROM bot_config WHERE bot_id = $1 AND active", bot_id)
    if row is None:
        return None
    config = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
    return {"version": row["version"], "config": config}


async def seed_initial_config(conn: asyncpg.Connection, bot: BotConfig) -> None:
    """Rule 1: bots/<name>.yaml seeds the initial DB row, once. A no-op if this bot
    already has an active version — the DB is authoritative from then on, the YAML
    is not re-read on every start."""
    from pmex_shadow.ops.registry import register_bot

    # Registration is unconditional, unlike the config seed below: a bot that
    # already has config from before the bots table existed still needs its
    # registry row, and register_bot won't un-archive a retired one.
    await register_bot(conn, bot.name)

    existing = await get_active_config(conn, bot.name)
    if existing is not None:
        return
    await conn.execute(
        "INSERT INTO bot_config (bot_id, version, config, active) VALUES ($1, 1, $2, true) "
        "ON CONFLICT (bot_id, version) DO NOTHING",
        bot.name, json.dumps(bot.model_dump(mode="json")),
    )


def _diff(old: dict | None, new: dict) -> dict:
    old = old or {}
    keys = set(old.keys()) | set(new.keys())
    return {k: {"from": old.get(k), "to": new.get(k)} for k in keys if old.get(k) != new.get(k)}


async def propose_config_update(conn: asyncpg.Connection, bot_id: str, actor: str, new_config_dict: dict) -> tuple[bool, str]:
    """Validate and, if valid, activate a new version. Returns (applied, message).
    A rejection always writes the audit row and never touches the active version.
    """
    current = await get_active_config(conn, bot_id)

    if new_config_dict.get("name") != bot_id:
        return await _reject(conn, bot_id, actor, current, new_config_dict, f"name must equal bot_id ({bot_id}); bot names are immutable (FR-O-6)")

    try:
        BotConfig.model_validate(new_config_dict)
    except ValidationError as exc:
        return await _reject(conn, bot_id, actor, current, new_config_dict, f"schema validation failed: {exc.errors()[0]['msg']}")

    # Sanity bounds (FR-C-4 examples): envelope must be positive and not absurd.
    envelope = new_config_dict.get("risk", {}).get("envelope_usd")
    try:
        if envelope is not None and float(envelope) <= 0:
            return await _reject(conn, bot_id, actor, current, new_config_dict, "risk.envelope_usd must be positive")
    except (TypeError, ValueError):
        return await _reject(conn, bot_id, actor, current, new_config_dict, "risk.envelope_usd is not a valid number")

    if current is not None:
        for field in NON_HOT_RELOADABLE_FIELDS:
            if new_config_dict.get(field) != current["config"].get(field):
                return await _reject(
                    conn, bot_id, actor, current, new_config_dict,
                    f"'{field}' is not hot-reloadable — requires a restart, not a params-screen change",
                )

    next_version = (current["version"] + 1) if current else 1
    async with conn.transaction():
        await conn.execute("UPDATE bot_config SET active = false WHERE bot_id = $1 AND active", bot_id)
        await conn.execute(
            "INSERT INTO bot_config (bot_id, version, config, active) VALUES ($1, $2, $3, true)",
            bot_id, next_version, json.dumps(new_config_dict),
        )
        await conn.execute(
            "INSERT INTO config_audit (bot_id, actor, from_version, to_version, diff, outcome) VALUES ($1, $2, $3, $4, $5, 'applied')",
            bot_id, actor, current["version"] if current else None, next_version,
            json.dumps(_diff(current["config"] if current else None, new_config_dict)),
        )
    return True, f"applied as version {next_version}"


async def _reject(conn: asyncpg.Connection, bot_id: str, actor: str, current: dict | None, new_config_dict: dict, reason: str) -> tuple[bool, str]:
    await conn.execute(
        "INSERT INTO config_audit (bot_id, actor, from_version, to_version, diff, outcome, reason) VALUES ($1, $2, $3, $3, $4, 'rejected', $5)",
        bot_id, actor, current["version"] if current else None,
        json.dumps(_diff(current["config"] if current else None, new_config_dict)), reason,
    )
    await conn.execute(
        "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'WARN', 'control.config', 'config change rejected', $2)",
        bot_id, json.dumps({"reason": reason, "actor": actor}),
    )
    return False, reason
