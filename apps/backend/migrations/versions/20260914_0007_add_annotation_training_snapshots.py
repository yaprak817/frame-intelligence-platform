"""add immutable annotation training snapshots

Revision ID: 20260914_0007
Revises: 20260909_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260914_0007"
down_revision: str | None = "20260909_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotation_training_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("selected_image_count", sa.Integer(), nullable=False),
        sa.Column("selected_class_count", sa.Integer(), nullable=False),
        sa.Column("selected_box_count", sa.Integer(), nullable=False),
        sa.Column("train_image_count", sa.Integer(), nullable=False),
        sa.Column("validation_image_count", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("snapshot_artifact_reference", sa.Text(), nullable=True),
        sa.Column("snapshot_artifact_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_artifact_sha256", sa.String(64), nullable=True),
        sa.Column("model_artifact_reference", sa.Text(), nullable=True),
        sa.Column("model_artifact_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("model_artifact_sha256", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "snapshot_version > 0", name="ck_annotation_training_runs_version"
        ),
        sa.CheckConstraint(
            "source_revision >= 0", name="ck_annotation_training_runs_revision"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SNAPSHOT_READY','SUCCEEDED','FAILED')",
            name="ck_annotation_training_runs_status",
        ),
        sa.CheckConstraint(
            "selected_image_count BETWEEN 50 AND 200",
            name="ck_annotation_training_runs_image_count",
        ),
        sa.CheckConstraint(
            "selected_class_count BETWEEN 1 AND 20",
            name="ck_annotation_training_runs_class_count",
        ),
        sa.CheckConstraint(
            "selected_box_count BETWEEN 1 AND 20000",
            name="ck_annotation_training_runs_box_count",
        ),
        sa.CheckConstraint(
            "train_image_count > 0 AND validation_image_count > 0 AND "
            "train_image_count + validation_image_count = selected_image_count",
            name="ck_annotation_training_runs_split_count",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_annotation_training_runs_fingerprint",
        ),
        sa.CheckConstraint(
            "config_hash ~ '^[0-9a-f]{64}$'",
            name="ck_annotation_training_runs_config_hash",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["annotation_projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "project_id", name="uq_annotation_training_runs_project"
        ),
        sa.UniqueConstraint(
            "project_id", "snapshot_version", name="uq_annotation_training_runs_version"
        ),
        sa.UniqueConstraint(
            "project_id",
            "source_revision",
            name="uq_annotation_training_runs_source_revision",
        ),
        sa.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_annotation_training_runs_idempotency",
        ),
        sa.UniqueConstraint(
            "project_id",
            "request_fingerprint",
            name="uq_annotation_training_runs_fingerprint",
        ),
    )
    op.create_index(
        "ix_annotation_training_runs_project_created",
        "annotation_training_runs",
        ["project_id", "created_at"],
    )
    op.create_index(
        "uq_annotation_training_runs_active_project",
        "annotation_training_runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING','RUNNING')"),
    )
    op.create_table(
        "annotation_training_snapshot_classes",
        sa.Column("training_id", sa.Uuid(), nullable=False),
        sa.Column("yolo_index", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.CheckConstraint(
            "yolo_index >= 0", name="ck_annotation_training_snapshot_classes_yolo"
        ),
        sa.ForeignKeyConstraint(
            ["training_id", "project_id"],
            ["annotation_training_runs.id", "annotation_training_runs.project_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("training_id", "yolo_index"),
        sa.UniqueConstraint(
            "training_id", "class_id", name="uq_annotation_training_snapshot_classes_id"
        ),
    )
    op.create_table(
        "annotation_training_snapshot_images",
        sa.Column("training_id", sa.Uuid(), nullable=False),
        sa.Column("image_index", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("source_object_key", sa.Text(), nullable=False),
        sa.Column("source_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_content_type", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("timestamp_ms", sa.BigInteger(), nullable=True),
        sa.Column("split", sa.String(5), nullable=False),
        sa.CheckConstraint(
            "image_index >= 0", name="ck_annotation_training_snapshot_images_index"
        ),
        sa.CheckConstraint(
            "split IN ('train','val')",
            name="ck_annotation_training_snapshot_images_split",
        ),
        sa.CheckConstraint(
            "width > 0 AND height > 0",
            name="ck_annotation_training_snapshot_images_dimensions",
        ),
        sa.CheckConstraint(
            "source_size_bytes > 0", name="ck_annotation_training_snapshot_images_size"
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_annotation_training_snapshot_images_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["training_id", "project_id"],
            ["annotation_training_runs.id", "annotation_training_runs.project_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("training_id", "image_index"),
    )
    op.create_table(
        "annotation_training_snapshot_boxes",
        sa.Column("training_id", sa.Uuid(), nullable=False),
        sa.Column("box_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("image_index", sa.Integer(), nullable=False),
        sa.Column("yolo_index", sa.Integer(), nullable=False),
        sa.Column("x_center", sa.Numeric(9, 8), nullable=False),
        sa.Column("y_center", sa.Numeric(9, 8), nullable=False),
        sa.Column("width", sa.Numeric(9, 8), nullable=False),
        sa.Column("height", sa.Numeric(9, 8), nullable=False),
        sa.CheckConstraint(
            "x_center >= 0 AND x_center <= 1",
            name="ck_annotation_training_snapshot_boxes_x",
        ),
        sa.CheckConstraint(
            "y_center >= 0 AND y_center <= 1",
            name="ck_annotation_training_snapshot_boxes_y",
        ),
        sa.CheckConstraint(
            "width > 0 AND width <= 1",
            name="ck_annotation_training_snapshot_boxes_width",
        ),
        sa.CheckConstraint(
            "height > 0 AND height <= 1",
            name="ck_annotation_training_snapshot_boxes_height",
        ),
        sa.CheckConstraint(
            "x_center - width / 2 >= 0 AND x_center + width / 2 <= 1",
            name="ck_annotation_training_snapshot_boxes_x_bounds",
        ),
        sa.CheckConstraint(
            "y_center - height / 2 >= 0 AND y_center + height / 2 <= 1",
            name="ck_annotation_training_snapshot_boxes_y_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["training_id", "project_id"],
            ["annotation_training_runs.id", "annotation_training_runs.project_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["training_id", "image_index"],
            [
                "annotation_training_snapshot_images.training_id",
                "annotation_training_snapshot_images.image_index",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["training_id", "yolo_index"],
            [
                "annotation_training_snapshot_classes.training_id",
                "annotation_training_snapshot_classes.yolo_index",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("training_id", "box_id"),
    )


def downgrade() -> None:
    present = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM annotation_training_runs LIMIT 1"))
        .first()
    )
    if present is not None:
        raise RuntimeError(
            "Downgrade refused while annotation training snapshots exist; "
            "remove them explicitly first"
        )
    op.drop_table("annotation_training_snapshot_boxes")
    op.drop_table("annotation_training_snapshot_images")
    op.drop_table("annotation_training_snapshot_classes")
    op.drop_table("annotation_training_runs")
