"""api_key token prefix

Revision ID: 3b9e7d2a6c41
Revises: 8f3c21b6d4ae
Create Date: 2026-10-21 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b9e7d2a6c41"
down_revision: str | None = "8f3c21b6d4ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows stay null: only the hash was ever stored, so their
    # prefix cannot be recovered.
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.add_column(sa.Column("token_prefix", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_column("token_prefix")
