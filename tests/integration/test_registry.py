"""Bot archival against a real Postgres. The property under test is the one that
motivated the bots table: archiving must remove a bot from the fleet view *without*
disturbing bot_config.active, because clearing `active` is what unsets the bot's
current config and makes its retained history unreachable.
"""

import datetime as dt
import json
import os

import asyncpg
import pytest

from pmex_shadow.control import queries
from pmex_shadow.ops.registry import (
    archive_blockers,
    archive_bot,
    list_bots,
    register_bot,
    unarchive_bot,
)

pytestmark = pytest.mark.skipif(
    "PMEX_TEST_DATABASE_URL" not in os.environ, reason="requires PMEX_TEST_DATABASE_URL (real Postgres)"
)

DATABASE_URL = os.environ.get("PMEX_TEST_DATABASE_URL", "")
BOT = "registry_test_bot"


@pytest.fixture
async def conn():
    c = await asyncpg.connect(DATABASE_URL)
    await _cleanup(c)
    await c.execute(
        "INSERT INTO bot_config (bot_id, version, config, active) VALUES ($1, 1, $2, true)",
        BOT, json.dumps({"name": BOT, "mode": "paper", "risk": {"envelope_usd": "500"}}),
    )
    await register_bot(c, BOT)
    yield c
    await _cleanup(c)
    await c.close()


async def _cleanup(c):
    await c.execute("DELETE FROM positions WHERE bot_id = $1", BOT)
    await c.execute("DELETE FROM bot_config WHERE bot_id = $1", BOT)
    await c.execute("DELETE FROM bots WHERE bot_id = $1", BOT)
    await c.execute("DELETE FROM heartbeats WHERE service = $1", f"bot:{BOT}")


async def _in_fleet(c) -> bool:
    view = await queries.fleet_view(c)
    return any(b["bot_id"] == BOT for b in view["bots"])


async def test_archive_hides_from_fleet_but_keeps_active_config(conn):
    assert await _in_fleet(conn)

    assert await archive_bot(conn, BOT, "retired in test")
    assert not await _in_fleet(conn)

    # The whole point: the config version pointer is untouched, so the bot stays
    # addressable and its history reachable.
    active = await conn.fetchrow("SELECT version FROM bot_config WHERE bot_id = $1 AND active", BOT)
    assert active is not None and active["version"] == 1

    # ...but it is no longer offered as somewhere to attach new targets.
    assert BOT not in await queries.list_bot_ids(conn)


async def test_unarchive_restores_to_fleet(conn):
    await archive_bot(conn, BOT, "retired in test")
    assert not await _in_fleet(conn)

    assert await unarchive_bot(conn, BOT)
    assert await _in_fleet(conn)
    assert BOT in await queries.list_bot_ids(conn)


async def test_archive_is_idempotent_and_keeps_first_timestamp(conn):
    await archive_bot(conn, BOT, "first reason")
    first = await conn.fetchval("SELECT archived_at FROM bots WHERE bot_id = $1", BOT)

    await archive_bot(conn, BOT, "second reason")
    row = await conn.fetchrow("SELECT archived_at, archived_reason FROM bots WHERE bot_id = $1", BOT)
    assert row["archived_at"] == first
    assert row["archived_reason"] == "first reason"


async def test_register_does_not_resurrect_an_archived_bot(conn):
    """A restart re-runs seed_initial_config, which registers unconditionally. That
    must not quietly pull a retired bot back into the fleet."""
    await archive_bot(conn, BOT, "retired in test")
    await register_bot(conn, BOT)
    assert not await _in_fleet(conn)


async def test_blockers_flag_running_bot(conn):
    assert await archive_blockers(conn, BOT) == []

    await conn.execute(
        "INSERT INTO heartbeats (service, at) VALUES ($1, now()) "
        "ON CONFLICT (service) DO UPDATE SET at = now()",
        f"bot:{BOT}",
    )
    assert any("still running" in b for b in await archive_blockers(conn, BOT))

    # A stale heartbeat is not a blocker — that's the case we're archiving for.
    await conn.execute(
        "UPDATE heartbeats SET at = $2 WHERE service = $1",
        f"bot:{BOT}", dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
    )
    assert await archive_blockers(conn, BOT) == []


async def test_blockers_flag_non_terminal_positions(conn):
    await conn.execute(
        "INSERT INTO positions (bot_id, token_id, shares, cost_basis_usd, lifecycle, mode) "
        "VALUES ($1, 'tok1', 10, 25.50, 'open', 'paper')",
        BOT,
    )
    assert any("not yet terminal" in b for b in await archive_blockers(conn, BOT))

    await conn.execute("UPDATE positions SET lifecycle = 'redeemed' WHERE bot_id = $1", BOT)
    assert await archive_blockers(conn, BOT) == []


async def test_show_archived_reveals_without_inflating_the_chips(conn):
    await archive_bot(conn, BOT, "retired in test")

    hidden = await queries.fleet_view(conn)
    assert all(b["bot_id"] != BOT for b in hidden["bots"])

    shown = await queries.fleet_view(conn, show_archived=True)
    assert any(b["bot_id"] == BOT for b in shown["bots"])

    # The chips describe the live fleet either way — toggling changes the table only.
    assert shown["summary"]["count"] == hidden["summary"]["count"]
    assert shown["summary"]["paper_count"] == hidden["summary"]["paper_count"]
    assert shown["summary"]["shown_count"] > hidden["summary"]["shown_count"]


async def test_status_filter_separates_stale_from_running(conn):
    await conn.execute(
        "INSERT INTO heartbeats (service, at) VALUES ($1, now()) "
        "ON CONFLICT (service) DO UPDATE SET at = now()",
        f"bot:{BOT}",
    )
    running = await queries.fleet_view(conn, status="running")
    assert any(b["bot_id"] == BOT for b in running["bots"])
    stale = await queries.fleet_view(conn, status="stale")
    assert all(b["bot_id"] != BOT for b in stale["bots"])

    await conn.execute(
        "UPDATE heartbeats SET at = $2 WHERE service = $1",
        f"bot:{BOT}", dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
    )
    assert any(b["bot_id"] == BOT for b in (await queries.fleet_view(conn, status="stale"))["bots"])
    assert all(b["bot_id"] != BOT for b in (await queries.fleet_view(conn, status="running"))["bots"])


async def test_mode_filter(conn):
    assert any(b["bot_id"] == BOT for b in (await queries.fleet_view(conn, mode="paper"))["bots"])
    assert all(b["bot_id"] != BOT for b in (await queries.fleet_view(conn, mode="live"))["bots"])


async def test_status_filters_exclude_archived_unless_asked(conn):
    """An archived bot is not "stale" or "running" — those describe the live fleet."""
    await archive_bot(conn, BOT, "retired in test")
    for st in ("running", "stale", "halted"):
        view = await queries.fleet_view(conn, status=st, show_archived=True)
        assert all(b["bot_id"] != BOT for b in view["bots"]), st

    view = await queries.fleet_view(conn, status="archived", show_archived=True)
    assert any(b["bot_id"] == BOT for b in view["bots"])


async def test_archive_unknown_bot_reports_failure(conn):
    assert not await archive_bot(conn, "no_such_bot_exists")
    assert not await unarchive_bot(conn, "no_such_bot_exists")
    assert all(b["bot_id"] != "no_such_bot_exists" for b in await list_bots(conn))
