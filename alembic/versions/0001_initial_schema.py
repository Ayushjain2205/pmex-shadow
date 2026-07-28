"""initial schema (PRD §5, verbatim)

Revision ID: 0001
Revises:
Create Date: 2026-07-29

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE target_fills (
          id              BIGSERIAL PRIMARY KEY,
          dedupe_key      TEXT NOT NULL UNIQUE,
          target          TEXT NOT NULL,
          token_id        TEXT NOT NULL,
          side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
          price           NUMERIC(18,6) NOT NULL,
          size            NUMERIC(24,6) NOT NULL,
          notional_usd    NUMERIC(24,6) NOT NULL,
          block_number    BIGINT,
          block_ts        TIMESTAMPTZ NOT NULL,
          detected_at     TIMESTAMPTZ NOT NULL,
          source          TEXT NOT NULL CHECK (source IN ('chain','dataapi')),
          raw             JSONB NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ON target_fills (target, block_ts DESC)")
    op.execute("CREATE INDEX ON target_fills (token_id)")

    op.execute(
        """
        CREATE TABLE intents (
          id                  BIGSERIAL PRIMARY KEY,
          bot_id              TEXT NOT NULL,
          dedupe_key          TEXT NOT NULL,
          fill_id             BIGINT NOT NULL REFERENCES target_fills(id),
          decision            TEXT NOT NULL CHECK (decision IN ('COPY','SKIP')),
          skip_reason         TEXT,
          token_id            TEXT NOT NULL,
          side                TEXT NOT NULL,
          target_price        NUMERIC(18,6) NOT NULL,
          intended_price      NUMERIC(18,6),
          intended_shares     NUMERIC(24,6),
          intended_usd        NUMERIC(24,6),
          target_percentile   NUMERIC(6,2),
          size_multiplier     NUMERIC(8,4),
          book_snapshot       JSONB,
          mode                TEXT NOT NULL CHECK (mode IN ('watch','paper','live')),
          created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (bot_id, dedupe_key)
        )
        """
    )
    op.execute("CREATE INDEX ON intents (bot_id, created_at DESC)")
    op.execute("CREATE INDEX ON intents (decision, skip_reason)")

    op.execute(
        """
        CREATE TABLE orders (
          id                BIGSERIAL PRIMARY KEY,
          bot_id            TEXT NOT NULL,
          intent_id         BIGINT NOT NULL REFERENCES intents(id),
          client_order_id   TEXT NOT NULL UNIQUE,
          exchange_order_id TEXT,
          state             TEXT NOT NULL CHECK (state IN
                              ('built','submitted','acked','partial','filled',
                               'cancelled','rejected','unknown','expired')),
          token_id          TEXT NOT NULL,
          side              TEXT NOT NULL,
          limit_price       NUMERIC(18,6) NOT NULL,
          shares            NUMERIC(24,6) NOT NULL,
          filled_shares     NUMERIC(24,6) NOT NULL DEFAULT 0,
          avg_fill_price    NUMERIC(18,6),
          mode              TEXT NOT NULL,
          error             TEXT,
          created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ON orders (bot_id, state)")

    op.execute(
        """
        CREATE TABLE order_transitions (
          id          BIGSERIAL PRIMARY KEY,
          order_id    BIGINT NOT NULL REFERENCES orders(id),
          from_state  TEXT,
          to_state    TEXT NOT NULL,
          detail      JSONB,
          at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE positions (
          id                BIGSERIAL PRIMARY KEY,
          bot_id            TEXT NOT NULL,
          token_id          TEXT NOT NULL,
          shares            NUMERIC(24,6) NOT NULL DEFAULT 0,
          cost_basis_usd    NUMERIC(24,6) NOT NULL DEFAULT 0,
          realized_pnl_usd  NUMERIC(24,6) NOT NULL DEFAULT 0,
          lifecycle         TEXT NOT NULL DEFAULT 'open' CHECK (lifecycle IN
                              ('open','pending_resolution','disputed','resolved',
                               'redeemed','voided','refunded','written_off')),
          condition_id      TEXT,
          neg_risk          BOOLEAN NOT NULL DEFAULT FALSE,
          opened_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_event_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          mode              TEXT NOT NULL,
          UNIQUE (bot_id, token_id, mode)
        )
        """
    )
    op.execute("CREATE INDEX ON positions (lifecycle)")

    op.execute(
        """
        CREATE TABLE bot_config (
          id          BIGSERIAL PRIMARY KEY,
          bot_id      TEXT NOT NULL,
          version     INTEGER NOT NULL,
          config      JSONB NOT NULL,
          active      BOOLEAN NOT NULL DEFAULT FALSE,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (bot_id, version)
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ON bot_config (bot_id) WHERE active")

    op.execute(
        """
        CREATE TABLE config_audit (
          id            BIGSERIAL PRIMARY KEY,
          bot_id        TEXT NOT NULL,
          actor         TEXT NOT NULL,
          from_version  INTEGER,
          to_version    INTEGER,
          diff          JSONB NOT NULL,
          outcome       TEXT NOT NULL CHECK (outcome IN ('applied','rejected')),
          reason        TEXT,
          at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE target_stats (
          target              TEXT PRIMARY KEY,
          alias               TEXT,
          size_p50            NUMERIC(24,6),
          size_p60            NUMERIC(24,6),
          size_p80            NUMERIC(24,6),
          size_p95            NUMERIC(24,6),
          fills_30d           INTEGER,
          hit_rate_30d        NUMERIC(6,4),
          pnl_30d_usd         NUMERIC(24,6),
          reversal_rate       NUMERIC(6,4),
          last_fill_at        TIMESTAMPTZ,
          status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                                ('shadow','active','paused_decay','paused_dormant','paused_manual')),
          computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE events (
          id        BIGSERIAL PRIMARY KEY,
          bot_id    TEXT,
          level     TEXT NOT NULL,
          component TEXT NOT NULL,
          message   TEXT NOT NULL,
          context   JSONB,
          at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ON events (bot_id, at DESC)")

    op.execute(
        """
        CREATE TABLE heartbeats (
          service   TEXT PRIMARY KEY,
          at        TIMESTAMPTZ NOT NULL,
          detail    JSONB
        )
        """
    )

    op.execute(
        """
        CREATE TABLE watcher_cursor (
          id                   INTEGER PRIMARY KEY CHECK (id = 1),
          last_processed_block BIGINT NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE backups (
          id          BIGSERIAL PRIMARY KEY,
          path        TEXT NOT NULL,
          bytes       BIGINT NOT NULL,
          succeeded   BOOLEAN NOT NULL,
          at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    for table in (
        "backups",
        "watcher_cursor",
        "heartbeats",
        "events",
        "target_stats",
        "config_audit",
        "bot_config",
        "positions",
        "order_transitions",
        "orders",
        "intents",
        "target_fills",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
