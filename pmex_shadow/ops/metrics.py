"""prometheus-client /metrics exposition (FR-O-4). Operational telemetry only —
detection lag, skip counts, queue depth, order states, heartbeat ages — never
financial figures (FR-C-1 draws that line explicitly: PnL/positions/fills come from
Postgres directly, sampled metrics are for ops, not money).

Computed fresh from Postgres on each scrape rather than maintained as live counters
in-process, since there's no single long-lived process that sees every bot — the
control plane is the one thing already reading across all of them for the fleet view.
"""

from __future__ import annotations

import asyncpg
from prometheus_client import CollectorRegistry, Gauge, generate_latest


async def render_metrics(database_url: str) -> bytes:
    registry = CollectorRegistry()

    heartbeat_age = Gauge("pmex_heartbeat_age_seconds", "Seconds since last heartbeat", ["service"], registry=registry)
    detection_lag = Gauge("pmex_detection_lag_seconds", "detected_at - block_ts, chain-sourced fills, last hour avg", registry=registry)
    skip_count = Gauge("pmex_skip_count", "Skip decisions in the last hour by reason", ["bot_id", "reason"], registry=registry)
    queue_depth = Gauge("pmex_queue_depth", "Orders stuck in built/submitted/unknown (not yet resolved)", ["bot_id"], registry=registry)
    order_states = Gauge("pmex_order_state_count", "Open orders by state, last 24h", ["bot_id", "state"], registry=registry)
    backup_age = Gauge("pmex_last_backup_age_seconds", "Seconds since the last successful backup", registry=registry)

    conn = await asyncpg.connect(database_url)
    try:
        for row in await conn.fetch("SELECT service, extract(epoch FROM now() - at) AS age FROM heartbeats"):
            heartbeat_age.labels(service=row["service"]).set(row["age"])

        lag_row = await conn.fetchrow(
            "SELECT avg(extract(epoch FROM (detected_at - block_ts))) AS avg_lag FROM target_fills "
            "WHERE source = 'chain' AND detected_at >= now() - interval '1 hour'"
        )
        if lag_row and lag_row["avg_lag"] is not None:
            detection_lag.set(lag_row["avg_lag"])

        for row in await conn.fetch(
            "SELECT bot_id, skip_reason, count(*) AS n FROM intents WHERE decision = 'SKIP' "
            "AND created_at >= now() - interval '1 hour' GROUP BY bot_id, skip_reason"
        ):
            skip_count.labels(bot_id=row["bot_id"], reason=row["skip_reason"] or "unknown").set(row["n"])

        for row in await conn.fetch(
            "SELECT bot_id, count(*) AS n FROM orders WHERE state IN ('built', 'submitted', 'unknown') GROUP BY bot_id"
        ):
            queue_depth.labels(bot_id=row["bot_id"]).set(row["n"])

        for row in await conn.fetch(
            "SELECT bot_id, state, count(*) AS n FROM orders WHERE created_at >= now() - interval '24 hours' GROUP BY bot_id, state"
        ):
            order_states.labels(bot_id=row["bot_id"], state=row["state"]).set(row["n"])

        backup_row = await conn.fetchrow(
            "SELECT extract(epoch FROM now() - at) AS age FROM backups WHERE succeeded ORDER BY at DESC LIMIT 1"
        )
        # Note for anyone alerting on this: a label-less prometheus_client Gauge
        # always reports a value (0.0 by default) even if .set() is never called —
        # "0 seconds since last backup" on a fresh install means "no backup has ever
        # run," not "one just succeeded." Confirmed live: this printed 0.0 against
        # an empty `backups` table. Alert on absence-of-recent-backup via `doctor`
        # (which distinguishes the two explicitly), not a bare threshold on this metric.
        if backup_row:
            backup_age.set(backup_row["age"])
    finally:
        await conn.close()

    return generate_latest(registry)
