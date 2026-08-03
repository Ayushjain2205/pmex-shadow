"""add bots registry with archived_at

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03

Bot identity had no home of its own: it was implied by the existence of a
bot_config row, and `active` there is a *version pointer* (see the partial unique
index on (bot_id) WHERE active) — not a lifecycle state. Retiring a bot by
clearing `active` therefore also unsets its current config, which breaks
list_bot_ids() and the runtime config load in execution/consumer.py — the bot
vanishes from the log/analysis pickers that its retained history is reachable
through. This table separates "is this bot part of the fleet" from "which config
version is current".
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE bots (
          bot_id           TEXT PRIMARY KEY,
          created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
          archived_at      TIMESTAMPTZ,
          archived_reason  TEXT,
          CONSTRAINT bots_archived_reason_requires_at CHECK (archived_reason IS NULL OR archived_at IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX ON bots (archived_at) WHERE archived_at IS NULL")

    # Backfill every bot that already has config history. created_at is the first
    # config version's timestamp, which is when seed_initial_config() would have
    # registered it had this table existed.
    op.execute(
        """
        INSERT INTO bots (bot_id, created_at)
        SELECT bot_id, min(created_at) FROM bot_config GROUP BY bot_id
        ON CONFLICT (bot_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS bots CASCADE")
