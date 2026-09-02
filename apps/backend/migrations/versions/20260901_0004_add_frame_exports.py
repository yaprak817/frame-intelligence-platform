"""add durable frame exports

Revision ID: 20260901_0004
Revises: 20260820_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20260901_0004"
down_revision = "20260820_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "frame_exports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("result_run_token", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("frame_indices", sa.JSON(), nullable=True),
        sa.Column("selection_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("artifact_reference", sa.Text(), nullable=True),
        sa.Column("artifact_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("artifact_sha256", sa.String(64), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_generation", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("working_object_key", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "result_run_token",
            "mode",
            "selection_hash",
            name="uq_frame_exports_selection",
        ),
    )
    op.create_index(
        "ix_frame_exports_job_created", "frame_exports", ["job_id", "created_at"]
    )
    op.create_table(
        "frame_export_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("export_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["export_id"], ["frame_exports.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("export_id"),
    )
    op.create_index(
        "ix_frame_export_outbox_ready",
        "frame_export_outbox",
        ["published_at", "next_attempt_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("frame_export_outbox")
    op.drop_table("frame_exports")
