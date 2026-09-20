"""add annotation inference execution

Revision ID: 20260916_0009
Revises: 20260914_0008
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0009"
down_revision: str | None = "20260914_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotation_inference_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("training_id", sa.Uuid(), nullable=False),
        sa.Column("model_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("target_image_count", sa.Integer(), nullable=False),
        sa.Column("processed_image_count", sa.Integer(), nullable=False),
        sa.Column("created_box_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "model_version > 0", name="ck_annotation_inference_runs_model_version"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')",
            name="ck_annotation_inference_runs_status",
        ),
        sa.CheckConstraint(
            "target_image_count > 0",
            name="ck_annotation_inference_runs_target_count",
        ),
        sa.CheckConstraint(
            "processed_image_count >= 0 AND "
            "processed_image_count <= target_image_count",
            name="ck_annotation_inference_runs_progress",
        ),
        sa.CheckConstraint(
            "created_box_count >= 0",
            name="ck_annotation_inference_runs_box_count",
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (failure_code IS NOT NULL)",
            name="ck_annotation_inference_runs_failure",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["annotation_projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["training_id", "project_id"],
            ["annotation_training_runs.id", "annotation_training_runs.project_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_annotation_inference_runs_project_created",
        "annotation_inference_runs",
        ["project_id", "created_at"],
    )
    op.create_index(
        "uq_annotation_inference_runs_active_project",
        "annotation_inference_runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING','RUNNING')"),
    )
    op.add_column(
        "annotation_boxes",
        sa.Column("auto_label_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_annotation_boxes_auto_label_run",
        "annotation_boxes",
        "annotation_inference_runs",
        ["auto_label_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_annotation_boxes_auto_label_run",
        "annotation_boxes",
        ["auto_label_run_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inference_present = bind.execute(
        sa.text("SELECT 1 FROM annotation_inference_runs LIMIT 1")
    ).first()
    training_present = bind.execute(
        sa.text("SELECT 1 FROM annotation_training_runs LIMIT 1")
    ).first()
    if inference_present is not None or training_present is not None:
        raise RuntimeError(
            "Downgrade refused while annotation inference or training data exists; "
            "remove it explicitly first"
        )
    op.drop_index("ix_annotation_boxes_auto_label_run", table_name="annotation_boxes")
    op.drop_constraint(
        "fk_annotation_boxes_auto_label_run",
        "annotation_boxes",
        type_="foreignkey",
    )
    op.drop_column("annotation_boxes", "auto_label_run_id")
    op.drop_index(
        "uq_annotation_inference_runs_active_project",
        table_name="annotation_inference_runs",
    )
    op.drop_index(
        "ix_annotation_inference_runs_project_created",
        table_name="annotation_inference_runs",
    )
    op.drop_table("annotation_inference_runs")
