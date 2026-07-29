"""Dead-man's switch (FR-EXE-7): if a bot is SIGKILLed with resting orders, they stay
live on the book with nothing supervising them. Two layers, per the PRD's own
guidance to prefer the native mechanism and keep a watchdog as backup:

1. **Native**: periodic `POST /heartbeats` (docs/VERIFIED.md item 4) — the exchange
   itself cancels all our open orders if these stop arriving. Survives a SIGKILL: the
   exchange doesn't need us alive to act, only silent.
2. **Local watchdog**: tracks whether our *own* heartbeat sends are actually
   succeeding, and proactively cancels if they've been failing — protection against
   the specific, documented complaint that the native mechanism's reliability is
   disputed upstream (`Polymarket/rs-clob-client` issue #239, noted in VERIFIED.md).
   This does NOT protect against total process death (a SIGKILLed process can't run
   its own watchdog) — that failure mode is exactly what layer 1 exists for.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import asyncpg

from pmex_shadow.execution.clob import send_heartbeat

logger = logging.getLogger("pmex_shadow.execution.deadman")


class DeadmanSwitch:
    def __init__(
        self,
        *,
        clob_base_url: str,
        bot_id: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        address: str,
        interval_s: int = 5,
        timeout_s: int = 30,
    ) -> None:
        self.clob_base_url = clob_base_url
        self.bot_id = bot_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.address = address
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.last_success: dt.datetime | None = None

    async def _send_once(self) -> bool:
        try:
            ok = await send_heartbeat(
                self.clob_base_url, api_key=self.api_key, secret=self.api_secret,
                passphrase=self.api_passphrase, address=self.address,
            )
            if ok:
                self.last_success = dt.datetime.now(dt.timezone.utc)
            return ok
        except Exception:
            logger.exception("heartbeat send failed for bot %s", self.bot_id)
            return False

    async def run(self, database_url: str, on_trip) -> None:
        """Runs forever. `on_trip` is called (no args, may be async) if our own
        heartbeat sends have been failing for longer than `timeout_s` — the local
        watchdog layer. Also logs a CRITICAL `events` row when it trips.
        """
        conn = await asyncpg.connect(database_url)
        try:
            while True:
                await self._send_once()
                if self.last_success is not None:
                    age_s = (dt.datetime.now(dt.timezone.utc) - self.last_success).total_seconds()
                    if age_s > self.timeout_s:
                        logger.critical(
                            "dead-man's switch tripped for bot %s: no successful heartbeat in %.1fs (limit %ds)",
                            self.bot_id, age_s, self.timeout_s,
                        )
                        await conn.execute(
                            "INSERT INTO events (bot_id, level, component, message, context) VALUES ($1, 'CRITICAL', 'execution.deadman', $2, $3)",
                            self.bot_id, "local watchdog tripped: own heartbeat sends failing",
                            f'{{"age_s": {age_s:.1f}, "timeout_s": {self.timeout_s}}}',
                        )
                        result = on_trip()
                        if asyncio.iscoroutine(result):
                            await result
                        self.last_success = dt.datetime.now(dt.timezone.utc)  # avoid re-tripping every tick
                await asyncio.sleep(self.interval_s)
        finally:
            await conn.close()
