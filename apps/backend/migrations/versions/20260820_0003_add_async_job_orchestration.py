"""Add asynchronous job orchestration state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0003"
down_revision: str | None = "20260820_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processing_jobs",
        sa.Column("result_summary", sa.JSON(none_as_null=True), nullable=True),
    )
    op.add_column("processing_jobs", sa.Column("run_token", sa.Uuid(), nullable=True))
    op.add_column(
        "processing_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_outbox",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE job_outbox SET next_attempt_at = created_at "
        "WHERE next_attempt_at IS NULL"
    )
    op.alter_column("job_outbox", "next_attempt_at", nullable=False)
    op.create_index(
        "ix_job_outbox_ready",
        "job_outbox",
        ["next_attempt_at", "created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_job_outbox_ready", table_name="job_outbox")
    op.drop_column("job_outbox", "next_attempt_at")
    op.drop_column("processing_jobs", "lease_expires_at")
    op.drop_column("processing_jobs", "run_token")
    op.drop_column("processing_jobs", "result_summary")
