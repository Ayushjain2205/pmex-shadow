"""WSS subscription to `OrderFilled` on CTF Exchange V2 + NegRisk Exchange V2,
topic-filtered to the union of watched targets (FR-W-1..5).

Uses raw JSON-RPC over `websockets` rather than web3.py's higher-level subscription
manager — this is standard Ethereum JSON-RPC (`eth_subscribe`/`eth_getLogs`), not
Polymarket-specific, so there's nothing here that needed empirical verification beyond
the OrderFilled decode itself (docs/VERIFIED.md items 2-3), and staying at the raw
JSON-RPC layer means exactly what's on the wire is known rather than trusted to a
library's internal subscription-management version churn.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

import asyncpg
import httpx
import websockets

from pmex_shadow.contracts import CTF_EXCHANGE_V2, NEG_RISK_EXCHANGE_V2, ORDER_FILLED_TOPIC0
from pmex_shadow.watcher.normalize import decode_order_filled_log, target_fill_from_chain_log

logger = logging.getLogger("pmex_shadow.watcher.chain")

EXCHANGE_ADDRESSES = [CTF_EXCHANGE_V2, NEG_RISK_EXCHANGE_V2]


def _address_to_topic(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


async def get_watched_targets(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT target FROM target_stats")
    return {r["target"].lower() for r in rows}


async def get_cursor(conn: asyncpg.Connection) -> int | None:
    row = await conn.fetchrow("SELECT last_processed_block FROM watcher_cursor WHERE id = 1")
    return row["last_processed_block"] if row else None


async def set_cursor(conn: asyncpg.Connection, block: int) -> None:
    """Advance the coverage cursor, never rewind it.

    GREATEST rather than a plain assignment because the reconnect backfill and the
    catch-up poll can both be writing here around a reconnect. Whoever finishes last
    would otherwise win, and a late writer carrying an older block would silently
    re-open a range already covered — cheap in itself (inserts dedupe), but it makes
    the cursor stop meaning "eth_getLogs has seen everything up to here", which is
    the one thing the rest of this module relies on it for.
    """
    await conn.execute(
        """
        INSERT INTO watcher_cursor (id, last_processed_block) VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE
            SET last_processed_block = GREATEST(watcher_cursor.last_processed_block, EXCLUDED.last_processed_block)
        """,
        block,
    )


async def insert_fill(conn: asyncpg.Connection, fill, raw: dict) -> int | None:
    """Insert a TargetFill; NOTIFY only on a genuine insert (FR-W-9). A conflict on the
    unique dedupe_key is a normal, harmless outcome (FR-W-6) — logged at DEBUG."""
    row = await conn.fetchrow(
        """
        INSERT INTO target_fills
            (dedupe_key, target, token_id, side, price, size, notional_usd,
             block_number, block_ts, detected_at, source, raw)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING id
        """,
        fill.dedupe_key, fill.target, fill.token_id, fill.side.value, fill.price, fill.size,
        fill.notional_usd, fill.block_number, fill.block_ts, fill.detected_at, fill.source,
        json.dumps(raw, default=str),
    )
    if row is None:
        logger.debug("duplicate fill skipped: %s", fill.dedupe_key)
        return None
    fill_id = row["id"]
    await conn.execute("SELECT pg_notify('pmex_fill', $1)", str(fill_id))
    return fill_id


async def _rpc(client: httpx.AsyncClient, http_url: str, method: str, params: list) -> object:
    resp = await client.post(http_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"{method} failed: {data['error']}")
    return data["result"]


async def backfill(
    client: httpx.AsyncClient,
    http_url: str,
    conn: asyncpg.Connection,
    targets: set[str],
    from_block: int,
    to_block: int,
    chunk_blocks: int,
) -> None:
    """Chunked eth_getLogs over [from_block, to_block] inclusive (FR-W-4). Shrinks the
    chunk on an oversized-range error rather than trusting a fixed cap (docs/VERIFIED.md
    item 11 — the real cap is provider-specific; item 15 — Alchemy's free tier
    specifically caps eth_getLogs at 10 blocks, confirmed from the actual error body,
    which is why the shrink floor here is 10 and not something more conservative-looking
    like 50 — 50 was still too high and left the watcher stuck retrying forever)."""
    if not targets:
        return
    maker_topics = [_address_to_topic(t) for t in targets]
    block_ts_cache: dict[int, dt.datetime] = {}
    start = from_block
    chunk = chunk_blocks

    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        try:
            logs = await _rpc(
                client, http_url, "eth_getLogs",
                [{
                    "address": EXCHANGE_ADDRESSES,
                    "topics": [ORDER_FILLED_TOPIC0, None, maker_topics],
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                }],
            )
        except (RuntimeError, httpx.HTTPStatusError) as exc:
            if chunk > 10:
                chunk = max(chunk // 2, 10)
                logger.warning("eth_getLogs range rejected (%s), shrinking chunk to %d blocks", exc, chunk)
                continue
            raise

        for log in logs:
            decoded = decode_order_filled_log(log)
            block_num = decoded["block_number"]
            if block_num not in block_ts_cache:
                block = await _rpc(client, http_url, "eth_getBlockByNumber", [hex(block_num), False])
                block_ts_cache[block_num] = dt.datetime.fromtimestamp(int(block["timestamp"], 16), tz=dt.timezone.utc)
            fill = target_fill_from_chain_log(
                decoded, decoded["maker"], block_ts_cache[block_num], dt.datetime.now(dt.timezone.utc)
            )
            if fill is not None:
                await insert_fill(conn, fill, log)

        await set_cursor(conn, end)
        start = end + 1


async def _resolve_urls(settings) -> tuple[str, str]:
    """Derive HTTP RPC and WS RPC URLs. Falls back to the WS URL's HTTP-scheme
    equivalent if a dedicated HTTP URL isn't configured, since eth_getLogs/eth_call
    work fine over either transport."""
    ws_url = settings.polygon_ws_url
    http_url = settings.polygon_rpc_url or ws_url.replace("wss://", "https://").replace("ws://", "http://")
    return http_url, ws_url


async def _watch_for_target_changes(targets_conn: asyncpg.Connection, current_targets: set[str], ws, interval_s: int = 15) -> None:
    """The live subscription's topic filter is fixed to whatever targets existed at
    connect time — `eth_subscribe` has no "update filter" call, so a target added
    (or removed) mid-connection is otherwise invisible to chain until the next
    disconnect/reconnect, which might not happen for hours. Poll for a change and
    force a clean resubscribe by closing the socket ourselves — `_subscribe_once`
    returning normally (not via exception) means the outer loop retries immediately,
    no backoff, no WARN failover event, since this isn't a failure."""
    while True:
        await asyncio.sleep(interval_s)
        latest = await get_watched_targets(targets_conn)
        if latest != current_targets:
            logger.info("target set changed (%d -> %d target(s)), reconnecting to pick it up", len(current_targets), len(latest))
            await ws.close()
            return


async def _catch_up_loop(
    client: httpx.AsyncClient,
    http_url: str,
    conn: asyncpg.Connection,
    targets: set[str],
    chunk_blocks: int,
    interval_s: float,
) -> None:
    """Poll eth_getLogs from the cursor to head, continuously, alongside the live
    subscription.

    The subscription is not reliable enough to be the only path. Providers drop it
    several times an hour (keepalive ping timeouts, server-side disconnects), and a
    dropped subscription is silent — the socket can stay open and simply stop
    delivering. Measured against a live provider, only 11 of 314 chain fills arrived
    within 5s; the rest surfaced in reconnect-backfill batches 10-30 minutes late, by
    which point a 5-minute market has resolved and has no book left to price against.

    Polling makes the subscription an optimization rather than a dependency: when it
    works, fills land in ~1s and this loop finds nothing new; when it dies, fills are
    still picked up within `interval_s`. Inserts dedupe on dedupe_key, so both paths
    finding the same fill is normal and only the first one NOTIFYs.

    This loop also owns the cursor (see `_subscribe_once`), which is what stops a
    brief disconnect from turning into a huge backfill.
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            head = int(await _rpc(client, http_url, "eth_blockNumber", []), 16)
            cursor = await get_cursor(conn)
            if cursor is None:
                await set_cursor(conn, head)
                continue
            if cursor >= head:
                continue
            # cursor is inclusive-covered, so resume at the next block — at this
            # cadence the range is normally one or two blocks.
            await backfill(client, http_url, conn, targets, cursor + 1, head, chunk_blocks)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a transient RPC error kill coverage — the next tick retries,
            # and the cursor not advancing is itself the durable signal that this is
            # failing, since it's what the staleness numbers are computed against.
            logger.exception("catch-up poll failed, retrying in %.1fs", interval_s)


async def _subscribe_once(ws_url: str, http_url: str, database_url: str, targets_conn: asyncpg.Connection, sub_conn: asyncpg.Connection, poll_conn: asyncpg.Connection, chunk_blocks: int, catchup_interval_s: float) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        targets = await get_watched_targets(targets_conn)
        head_hex = await _rpc(client, http_url, "eth_blockNumber", [])
        head = int(head_hex, 16)

        cursor = await get_cursor(targets_conn)
        if cursor is not None and cursor < head:
            logger.info("backfilling blocks %d..%d before subscribing", cursor, head)
            await backfill(client, http_url, targets_conn, targets, cursor, head, chunk_blocks)
        elif cursor is None:
            await set_cursor(targets_conn, head)

        if not targets:
            # An empty list at a topic position means "no constraint" (matches
            # EVERY OrderFilled log), not "matches nothing" — confirmed empirically
            # against a live provider while building this. Subscribing with zero
            # targets would silently ingest the entire exchange's fill stream.
            # Idle instead: wait for a target to be registered before subscribing.
            logger.info("no targets configured yet — waiting before subscribing")
            await asyncio.sleep(10)
            return

        maker_topics = [_address_to_topic(t) for t in targets]
        async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
            sub_request = {
                "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                "params": ["logs", {"address": EXCHANGE_ADDRESSES, "topics": [ORDER_FILLED_TOPIC0, None, maker_topics]}],
            }
            await ws.send(json.dumps(sub_request))
            ack = json.loads(await ws.recv())
            if "result" not in ack:
                raise RuntimeError(f"eth_subscribe failed: {ack}")
            logger.info("subscribed to OrderFilled logs (%d targets)", len(targets))

            watch_task = asyncio.create_task(_watch_for_target_changes(targets_conn, targets, ws))
            catch_up_task = asyncio.create_task(
                _catch_up_loop(client, http_url, poll_conn, targets, chunk_blocks, catchup_interval_s)
            )
            try:
                block_ts_cache: dict[int, dt.datetime] = {}
                async for message in ws:
                    msg = json.loads(message)
                    if msg.get("method") != "eth_subscription":
                        continue
                    log = msg["params"]["result"]
                    decoded = decode_order_filled_log(log)
                    block_num = decoded["block_number"]
                    if block_num not in block_ts_cache:
                        block = await _rpc(client, http_url, "eth_getBlockByNumber", [hex(block_num), False])
                        block_ts_cache[block_num] = dt.datetime.fromtimestamp(int(block["timestamp"], 16), tz=dt.timezone.utc)
                        block_ts_cache = dict(list(block_ts_cache.items())[-64:])  # bounded cache
                    fill = target_fill_from_chain_log(
                        decoded, decoded["maker"], block_ts_cache[block_num], dt.datetime.now(dt.timezone.utc)
                    )
                    if fill is not None:
                        await insert_fill(sub_conn, fill, log)
                    # Deliberately does NOT advance the cursor. This path only sees
                    # blocks that happen to contain a matching log, so letting it
                    # write the cursor made coverage a function of how often the
                    # targets traded: a quiet 20 minutes left the cursor 20 minutes
                    # stale, and the next disconnect — however brief — had to
                    # backfill all of it, which is why fills were surfacing in
                    # half-hour-old batches. Worse, a log arriving from a block ahead
                    # of unscanned ones marked them covered without ever reading
                    # them. The catch-up poll owns the cursor now, so it advances
                    # with the chain rather than with the target's activity.
            finally:
                for task in (watch_task, catch_up_task):
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass


async def run_chain_watcher(settings) -> None:
    """Reconnect-forever loop with fallback RPC failover (FR-W-5) and backfill-on-gap
    (FR-W-3/4). Every disconnect is logged as an `events` row at WARN — not just the
    ones that trigger failover — since `logger.warning()` alone doesn't survive a
    container restart, and a reconnect this loop treats as routine is exactly what
    later produces a stale, unevaluated backfill batch downstream (found investigating
    why 69% of fills for some targets had no intents row: median chain-side lag on
    those was ~17 minutes, i.e. backfill catch-up after a reconnect, not real-time
    processing — and there was no durable record of why any given reconnect happened)."""
    http_url, primary_ws = await _resolve_urls(settings)
    fallback_ws = settings.polygon_ws_url_fallback

    targets_conn = await asyncpg.connect(settings.database_url)
    sub_conn = await asyncpg.connect(settings.database_url)
    # The catch-up poll runs concurrently with both the subscription loop and the
    # target-change watcher, and an asyncpg connection can't be shared across
    # concurrent queries — so it gets its own.
    poll_conn = await asyncpg.connect(settings.database_url)
    try:
        use_fallback = False
        backoff_s = 1
        while True:
            ws_url = fallback_ws if (use_fallback and fallback_ws) else primary_ws
            try:
                await _subscribe_once(
                    ws_url, http_url, settings.database_url, targets_conn, sub_conn, poll_conn,
                    settings.backfill_chunk_blocks, settings.chain_catchup_interval_s,
                )
                backoff_s = 1
            except Exception as exc:
                logger.warning("chain watcher disconnected (%s), reconnecting in %ds", exc, backoff_s)
                await targets_conn.execute(
                    "INSERT INTO events (level, component, message, context) VALUES ('WARN', 'watcher.chain', $1, $2)",
                    "disconnected, reconnecting",
                    json.dumps({"ws_url": ws_url, "using_fallback": use_fallback, "backoff_s": backoff_s, "error": str(exc)}),
                )
                if not use_fallback and fallback_ws:
                    use_fallback = True
                    await targets_conn.execute(
                        "INSERT INTO events (level, component, message, context) VALUES ('WARN', 'watcher.chain', $1, $2)",
                        "failing over to fallback RPC", json.dumps({"primary": primary_ws, "fallback": fallback_ws, "error": str(exc)}),
                    )
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 30)
    finally:
        await targets_conn.close()
        await sub_conn.close()
        await poll_conn.close()
