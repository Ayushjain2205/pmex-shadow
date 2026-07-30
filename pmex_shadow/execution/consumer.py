"""Connects the shared watcher's fill stream to one bot's policy decisions and, for
Intents, the execution router's queue. LISTENs on `pmex_fill` (FR-W-9) rather than
polling — the whole point of Postgres LISTEN/NOTIFY as the fan-out mechanism
(design doc §2: "no Kafka, no Redis").
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections import defaultdict
from decimal import Decimal

import asyncpg

from pmex_shadow.config import BotConfig, PolicyFile
from pmex_shadow.execution.router import ExecutionRouter, insert_intent_row
from pmex_shadow.ledger.subaccount import get_ledger_state
from pmex_shadow.market.cache import get_market_meta, get_orderbook
from pmex_shadow.models import BookSnapshot, Intent, Side, TargetFill, TargetPolicyStats
from pmex_shadow.policy.engine import decide

logger = logging.getLogger("pmex_shadow.execution.consumer")

_BOOK_HISTORY_WINDOW = dt.timedelta(minutes=5)
_BOOK_HISTORY_MAX_LEN = 50


class BotConsumer:
    def __init__(
        self,
        *,
        bot: BotConfig,
        policy_file: PolicyFile,
        database_url: str,
        gamma_api_base_url: str,
        clob_base_url: str,
        router: ExecutionRouter,
    ) -> None:
        self.bot = bot
        self.policy_file = policy_file
        self.policy = policy_file.profiles[bot.policy.profile]
        self.global_risk = policy_file.risk
        self.database_url = database_url
        self.gamma_api_base_url = gamma_api_base_url
        self.clob_base_url = clob_base_url
        self.router = router
        self._book_history: dict[str, list[BookSnapshot]] = defaultdict(list)
        self._market_cache: dict[str, object] = {}
        self._target_addresses: set[str] = set()
        self._config_version: int | None = None

    async def run(self) -> None:
        listen_conn = await asyncpg.connect(self.database_url)
        work_conn = await asyncpg.connect(self.database_url)
        config_conn = await asyncpg.connect(self.database_url)
        await self._refresh_targets(work_conn)  # from the YAML-seeded `bot` passed to __init__; corrected below once the DB's active version (which may have since diverged) loads
        await self._load_config_version(config_conn, initial=True)

        queue: asyncio.Queue[int] = asyncio.Queue()

        def _on_notify(_conn, _pid, _channel, payload: str) -> None:
            queue.put_nowait(int(payload))

        await listen_conn.add_listener("pmex_fill", _on_notify)
        logger.info("bot %s consumer listening on pmex_fill for %d target(s)", self.bot.name, len(self._target_addresses))
        reload_task = asyncio.create_task(self._poll_config_reload(config_conn))
        try:
            while True:
                fill_id = await queue.get()
                try:
                    await self._handle_fill(work_conn, fill_id)
                except Exception:
                    logger.exception("failed to process fill %d for bot %s", fill_id, self.bot.name)
        finally:
            reload_task.cancel()
            await listen_conn.remove_listener("pmex_fill", _on_notify)
            await listen_conn.close()
            await work_conn.close()
            await config_conn.close()

    async def _load_config_version(self, conn: asyncpg.Connection, initial: bool = False) -> None:
        """FR-C-3: bots poll their active bot_config version and hot-reload.
        `targets` is hot-reloadable (control/config_write.py no longer rejects a
        diff on it) — wallet and name are the only fields still restart-required,
        since those are bound to a live signing client at process start
        (cli.py's ClobClient/ExecutionRouter construction), not just an in-process
        set of addresses."""
        row = await conn.fetchrow("SELECT version, config FROM bot_config WHERE bot_id = $1 AND active", self.bot.name)
        if row is None:
            return
        if not initial and row["version"] == self._config_version:
            return

        config_dict = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
        try:
            new_bot = BotConfig.model_validate(config_dict)
        except Exception:
            logger.exception("bot %s: active bot_config version %d failed to parse, ignoring", self.bot.name, row["version"])
            return

        if not initial and row["version"] != self._config_version:
            logger.info("bot %s: hot-reloaded config v%d -> v%d", self.bot.name, self._config_version, row["version"])

        targets_changed = new_bot.targets != self.bot.targets
        self.bot = new_bot
        self.policy = self.policy_file.profiles[new_bot.policy.profile]
        self._config_version = row["version"]

        if initial or targets_changed:
            await self._refresh_targets(conn)
            if not initial:
                logger.info("bot %s: target list changed, now watching %d target(s)", self.bot.name, len(self._target_addresses))

    async def _poll_config_reload(self, conn: asyncpg.Connection, interval_s: int = 10) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self._load_config_version(conn)
            except Exception:
                logger.exception("bot %s: config reload check failed", self.bot.name)

    async def _refresh_targets(self, conn: asyncpg.Connection) -> None:
        addresses = []
        for t in self.bot.targets:
            if t.lower().startswith("0x"):
                addresses.append(t.lower())
                continue
            row = await conn.fetchrow("SELECT target FROM target_stats WHERE alias = $1", t)
            if row:
                addresses.append(row["target"])
            else:
                logger.warning(
                    "bot %s: targets alias %r not found in target_stats — dropped, not watched "
                    "(add it with `targets add <address> --alias %s` first)",
                    self.bot.name, t, t,
                )
        self._target_addresses = set(addresses)

    async def _handle_fill(self, conn: asyncpg.Connection, fill_id: int) -> None:
        row = await conn.fetchrow(
            "SELECT id, dedupe_key, target, token_id, side, price, size, notional_usd, "
            "block_number, block_ts, detected_at, source FROM target_fills WHERE id = $1",
            fill_id,
        )
        if row is None or row["target"] not in self._target_addresses:
            return

        fill = TargetFill(
            dedupe_key=row["dedupe_key"], target=row["target"], token_id=row["token_id"],
            side=Side.BUY if row["side"] == "BUY" else Side.SELL, price=row["price"], size=row["size"],
            notional_usd=row["notional_usd"], block_number=row["block_number"], block_ts=row["block_ts"],
            detected_at=row["detected_at"], source=row["source"],
        )

        book = await get_orderbook(self.clob_base_url, fill.token_id)
        if book is None:
            logger.debug("no orderbook for %s, cannot evaluate fill %d", fill.token_id, fill_id)
            return

        if fill.token_id not in self._market_cache:
            self._market_cache[fill.token_id] = await get_market_meta(self.gamma_api_base_url, fill.token_id)
        market = self._market_cache[fill.token_id]
        if market is None:
            return

        target_row = await conn.fetchrow(
            "SELECT size_p50, size_p60, size_p80, size_p95, status FROM target_stats WHERE target = $1", fill.target,
        )
        if target_row is None:
            return

        position_before = await self._target_position_before(conn, fill, fill_id)
        target_stats = TargetPolicyStats(
            size_p50=target_row["size_p50"] or Decimal(0), size_p60=target_row["size_p60"] or Decimal(0),
            size_p80=target_row["size_p80"] or Decimal(0), size_p95=target_row["size_p95"] or Decimal(0),
            status=target_row["status"], position_before=position_before,
        )

        ledger = await get_ledger_state(conn, self.bot.name, self.bot.mode)

        history = self._book_history[fill.token_id]
        now = dt.datetime.now(dt.timezone.utc)
        window = tuple(b for b in history if now - b.taken_at <= _BOOK_HISTORY_WINDOW)

        decision = decide(
            fill=fill, book=book, book_history=window, bot=self.bot, policy=self.policy,
            global_risk=self.global_risk, ledger=ledger, target=target_stats, market=market, now=now,
        )

        history.append(book)
        if len(history) > _BOOK_HISTORY_MAX_LEN:
            del history[: len(history) - _BOOK_HISTORY_MAX_LEN]

        intent_id = await insert_intent_row(conn, decision, fill_id, self.bot.mode)
        if intent_id is None:
            return  # duplicate delivery, already processed (FR-EXE-2)

        if isinstance(decision, Intent):
            await self.router.enqueue(decision, intent_id)

    async def _target_position_before(self, conn: asyncpg.Connection, fill: TargetFill, fill_id: int) -> Decimal:
        """FR-P-11 needs the target's own share count in this token immediately
        before this fill — summed from their own fill history, not tracked anywhere
        directly (see TargetPolicyStats docstring in models.py). Ties on block_ts
        broken by `id` (insertion order) since two fills can share a block.
        """
        row = await conn.fetchrow(
            """
            SELECT COALESCE(sum(CASE WHEN side = 'BUY' THEN size ELSE -size END), 0) AS net
            FROM target_fills
            WHERE target = $1 AND token_id = $2 AND (block_ts < $3 OR (block_ts = $3 AND id < $4))
            """,
            fill.target, fill.token_id, fill.block_ts, fill_id,
        )
        return row["net"] if row else Decimal(0)
