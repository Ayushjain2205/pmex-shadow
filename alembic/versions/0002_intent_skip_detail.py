"""add intents.skip_detail

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-31

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE intents ADD COLUMN skip_detail JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE intents DROP COLUMN skip_detail")
