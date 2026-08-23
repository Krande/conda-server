"""maintenance jobs table

Revision ID: 8f3c21b6d4ae
Revises: 5781ad0717a3
Create Date: 2026-08-23 10:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f3c21b6d4ae"
down_revision: str | None = "5781ad0717a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("with_metadata_count", sa.Integer(), nullable=False),
        sa.Column("current_target", sa.String(length=512), nullable=True),
        sa.Column("error", sa.String(length=2048), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_maintenance_jobs_kind", "maintenance_jobs", ["kind"])
    op.create_index("ix_maintenance_jobs_channel_id", "maintenance_jobs", ["channel_id"])
    op.create_index("ix_maintenance_jobs_user_id", "maintenance_jobs", ["user_id"])
    op.create_index("ix_maintenance_jobs_status", "maintenance_jobs", ["status"])

    # Mirrors the Index declared on PackageVersion. Only the rows nobody
    # has inspected yet are indexed, so this stays proportional to the
    # work left rather than to the size of the channel — and disappears
    # once a channel is fully backfilled. Partial-index syntax is
    # dialect-specific; the two the project targets both support it.
    op.create_index(
        "ix_package_versions_about_pending",
        "package_versions",
        ["package_id"],
        postgresql_where=sa.text("about_fetched_at IS NULL"),
        sqlite_where=sa.text("about_fetched_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_package_versions_about_pending", table_name="package_versions")
    op.drop_index("ix_maintenance_jobs_status", table_name="maintenance_jobs")
    op.drop_index("ix_maintenance_jobs_user_id", table_name="maintenance_jobs")
    op.drop_index("ix_maintenance_jobs_channel_id", table_name="maintenance_jobs")
    op.drop_index("ix_maintenance_jobs_kind", table_name="maintenance_jobs")
    op.drop_table("maintenance_jobs")
