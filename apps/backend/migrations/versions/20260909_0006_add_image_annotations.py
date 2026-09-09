"""add image annotation core

Revision ID: 20260909_0006
Revises: 20260902_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0006"
down_revision: str | None = "20260902_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotation_projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("result_run_token", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_annotation_projects_revision"),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "result_run_token", name="uq_annotation_projects_job_run"
        ),
    )
    op.create_index("ix_annotation_projects_job", "annotation_projects", ["job_id"])
    op.create_table(
        "annotation_classes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("yolo_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("normalized_name", sa.String(80), nullable=False),
        sa.Column("color", sa.CHAR(7), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("yolo_index >= 0", name="ck_annotation_classes_yolo_index"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["annotation_projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "id", name="uq_annotation_classes_project_id"
        ),
        sa.UniqueConstraint(
            "project_id", "yolo_index", name="uq_annotation_classes_yolo_index"
        ),
        sa.UniqueConstraint(
            "project_id",
            "normalized_name",
            name="uq_annotation_classes_normalized_name",
        ),
    )
    op.create_table(
        "annotation_images",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("image_index", sa.Integer(), nullable=False),
        sa.Column("image_filename", sa.String(255), nullable=False),
        sa.Column("image_sha256", sa.CHAR(64), nullable=False),
        sa.Column("yolo_sha256", sa.CHAR(64), nullable=False),
        sa.Column("completed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("image_index >= 0", name="ck_annotation_images_index"),
        sa.CheckConstraint(
            "image_sha256 ~ '^[0-9a-f]{64}$'", name="ck_annotation_images_sha256"
        ),
        sa.CheckConstraint(
            "yolo_sha256 ~ '^[0-9a-f]{64}$'", name="ck_annotation_images_yolo_sha256"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["annotation_projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("project_id", "image_index"),
    )
    op.create_table(
        "annotation_boxes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("image_index", sa.Integer(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("x_center", sa.Numeric(9, 8), nullable=False),
        sa.Column("y_center", sa.Numeric(9, 8), nullable=False),
        sa.Column("width", sa.Numeric(9, 8), nullable=False),
        sa.Column("height", sa.Numeric(9, 8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "x_center >= 0 AND x_center <= 1", name="ck_annotation_boxes_x_center"
        ),
        sa.CheckConstraint(
            "y_center >= 0 AND y_center <= 1", name="ck_annotation_boxes_y_center"
        ),
        sa.CheckConstraint(
            "width > 0 AND width <= 1", name="ck_annotation_boxes_width"
        ),
        sa.CheckConstraint(
            "height > 0 AND height <= 1", name="ck_annotation_boxes_height"
        ),
        sa.CheckConstraint(
            "x_center - width / 2 >= 0 AND x_center + width / 2 <= 1",
            name="ck_annotation_boxes_x_bounds",
        ),
        sa.CheckConstraint(
            "y_center - height / 2 >= 0 AND y_center + height / 2 <= 1",
            name="ck_annotation_boxes_y_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "image_index"],
            ["annotation_images.project_id", "annotation_images.image_index"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "class_id"],
            ["annotation_classes.project_id", "annotation_classes.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id", name="uq_annotation_boxes_project_id"),
    )
    op.create_index(
        "ix_annotation_boxes_project_image",
        "annotation_boxes",
        ["project_id", "image_index"],
    )
    op.create_index(
        "ix_annotation_boxes_project_class",
        "annotation_boxes",
        ["project_id", "class_id"],
    )


def downgrade() -> None:
    present = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM annotation_projects LIMIT 1"))
        .first()
    )
    if present is not None:
        raise RuntimeError(
            "Downgrade refused while annotation data exists; remove it explicitly first"
        )
    op.drop_table("annotation_boxes")
    op.drop_table("annotation_images")
    op.drop_table("annotation_classes")
    op.drop_table("annotation_projects")
