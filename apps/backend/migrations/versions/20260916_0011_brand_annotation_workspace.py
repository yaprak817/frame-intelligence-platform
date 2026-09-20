"""add brand-level annotation source mapping

Revision ID: 20260916_0011
Revises: 20260916_0010
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "20260916_0011"
down_revision = "20260916_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "annotation_projects",
        sa.Column("brand_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_annotation_projects_brand_id",
        "annotation_projects",
        "brands",
        ["brand_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_annotation_projects_brand_id",
        "annotation_projects",
        ["brand_id"],
    )
    op.create_index(
        "ix_annotation_projects_brand",
        "annotation_projects",
        ["brand_id"],
        unique=False,
    )

    op.add_column(
        "annotation_images",
        sa.Column("source_job_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "annotation_images",
        sa.Column("source_result_run_token", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "annotation_images",
        sa.Column("source_image_index", sa.Integer(), nullable=True),
    )

    op.create_foreign_key(
        "fk_annotation_images_source_job_id",
        "annotation_images",
        "processing_jobs",
        ["source_job_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_annotation_images_source_index",
        "annotation_images",
        "source_image_index IS NULL OR source_image_index >= 0",
    )
    op.create_unique_constraint(
        "uq_annotation_images_source",
        "annotation_images",
        [
            "project_id",
            "source_job_id",
            "source_result_run_token",
            "source_image_index",
        ],
    )
    op.create_index(
        "ix_annotation_images_source_job",
        "annotation_images",
        ["source_job_id", "source_image_index"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_annotation_images_source_job", table_name="annotation_images")
    op.drop_constraint(
        "uq_annotation_images_source",
        "annotation_images",
        type_="unique",
    )
    op.drop_constraint(
        "ck_annotation_images_source_index",
        "annotation_images",
        type_="check",
    )
    op.drop_constraint(
        "fk_annotation_images_source_job_id",
        "annotation_images",
        type_="foreignkey",
    )
    op.drop_column("annotation_images", "source_image_index")
    op.drop_column("annotation_images", "source_result_run_token")
    op.drop_column("annotation_images", "source_job_id")

    op.drop_index("ix_annotation_projects_brand", table_name="annotation_projects")
    op.drop_constraint(
        "uq_annotation_projects_brand_id",
        "annotation_projects",
        type_="unique",
    )
    op.drop_constraint(
        "fk_annotation_projects_brand_id",
        "annotation_projects",
        type_="foreignkey",
    )
    op.drop_column("annotation_projects", "brand_id")
