# ruff: noqa: E501
"""add bounded annotation training execution

Revision ID: 20260914_0008
Revises: 20260914_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260914_0008"
down_revision: str | None = "20260914_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "annotation_training_runs",
        sa.Column(
            "progress_completed", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "annotation_training_runs",
        sa.Column("progress_total", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "annotation_training_runs", sa.Column("lease_token", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "annotation_training_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "annotation_training_runs",
        sa.Column(
            "attempt_generation", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "annotation_training_runs",
        sa.Column("working_prefix", sa.Text(), nullable=True),
    )
    op.add_column(
        "annotation_training_runs",
        sa.Column("model_version", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_annotation_training_runs_progress",
        "annotation_training_runs",
        "progress_total >= 0 AND progress_completed >= 0 AND progress_completed <= progress_total",
    )
    op.create_check_constraint(
        "ck_annotation_training_runs_attempt",
        "annotation_training_runs",
        "attempt_generation >= 0",
    )
    op.create_check_constraint(
        "ck_annotation_training_runs_snapshot_size",
        "annotation_training_runs",
        "snapshot_artifact_size_bytes IS NULL OR snapshot_artifact_size_bytes BETWEEN 1 AND 1073741824",
    )
    op.create_check_constraint(
        "ck_annotation_training_runs_model_size",
        "annotation_training_runs",
        "model_artifact_size_bytes IS NULL OR model_artifact_size_bytes BETWEEN 1 AND 268435456",
    )
    op.create_check_constraint(
        "ck_annotation_training_runs_snapshot_sha",
        "annotation_training_runs",
        "snapshot_artifact_sha256 IS NULL OR snapshot_artifact_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_annotation_training_runs_model_sha",
        "annotation_training_runs",
        "model_artifact_sha256 IS NULL OR model_artifact_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_annotation_training_runs_failure",
        "annotation_training_runs",
        "(status = 'FAILED') = (failure_code IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_annotation_training_runs_success",
        "annotation_training_runs",
        "status <> 'SUCCEEDED' OR (model_version IS NOT NULL AND model_artifact_reference IS NOT NULL AND model_artifact_size_bytes IS NOT NULL AND model_artifact_sha256 IS NOT NULL AND snapshot_artifact_reference IS NOT NULL AND snapshot_artifact_size_bytes IS NOT NULL AND snapshot_artifact_sha256 IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_annotation_training_runs_model_version",
        "annotation_training_runs",
        ["project_id", "model_version"],
    )
    op.create_table(
        "annotation_training_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("training_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type = 'START_ANNOTATION_TRAINING'",
            name="ck_annotation_training_outbox_event",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_annotation_training_outbox_attempt"
        ),
        sa.ForeignKeyConstraint(
            ["training_id"], ["annotation_training_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "training_id", "event_type", name="uq_annotation_training_outbox_event"
        ),
    )
    op.create_index(
        "ix_annotation_training_outbox_ready",
        "annotation_training_outbox",
        ["next_attempt_at", "created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    execution_present = bind.execute(
        sa.text("SELECT 1 FROM annotation_training_outbox LIMIT 1")
    ).first()
    execution_present = (
        execution_present
        or bind.execute(
            sa.text(
                "SELECT 1 FROM annotation_training_runs WHERE status <> 'SNAPSHOT_READY' OR progress_completed <> 0 OR progress_total <> 0 OR lease_token IS NOT NULL OR lease_expires_at IS NOT NULL OR attempt_generation <> 0 OR working_prefix IS NOT NULL OR model_version IS NOT NULL OR snapshot_artifact_reference IS NOT NULL OR model_artifact_reference IS NOT NULL LIMIT 1"
            )
        ).first()
    )
    if execution_present is not None:
        raise RuntimeError(
            "Downgrade refused while annotation training execution data exists"
        )
    op.drop_index(
        "ix_annotation_training_outbox_ready", table_name="annotation_training_outbox"
    )
    op.drop_table("annotation_training_outbox")
    op.drop_constraint(
        "uq_annotation_training_runs_model_version",
        "annotation_training_runs",
        type_="unique",
    )
    for name in (
        "ck_annotation_training_runs_success",
        "ck_annotation_training_runs_failure",
        "ck_annotation_training_runs_model_sha",
        "ck_annotation_training_runs_snapshot_sha",
        "ck_annotation_training_runs_model_size",
        "ck_annotation_training_runs_snapshot_size",
        "ck_annotation_training_runs_attempt",
        "ck_annotation_training_runs_progress",
    ):
        op.drop_constraint(name, "annotation_training_runs", type_="check")
    for name in (
        "model_version",
        "working_prefix",
        "attempt_generation",
        "lease_expires_at",
        "lease_token",
        "progress_total",
        "progress_completed",
    ):
        op.drop_column("annotation_training_runs", name)
