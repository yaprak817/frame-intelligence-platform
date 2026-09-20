"""add brand workspace

Revision ID: 20260916_0010
Revises: 20260916_0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0010"
down_revision: str | None = "20260916_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("normalized_name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), server_default="DRAFT", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_name", name="uq_brands_normalized_name"),
    )
    op.create_index("ix_brands_updated_at", "brands", ["updated_at"])

    op.create_table(
        "brand_classes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("normalized_name", sa.String(80), nullable=False),
        sa.Column("color", sa.CHAR(7), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "brand_id", "normalized_name", name="uq_brand_classes_brand_normalized_name"
        ),
    )
    op.create_index("ix_brand_classes_brand", "brand_classes", ["brand_id"])

    op.create_table(
        "brand_datasets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("annotation_project_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["processing_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["annotation_project_id"], ["annotation_projects.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "annotation_project_id", name="uq_brand_datasets_annotation_project"
        ),
        sa.UniqueConstraint("job_id", name="uq_brand_datasets_job"),
    )
    op.create_index("ix_brand_datasets_brand", "brand_datasets", ["brand_id"])


def downgrade() -> None:
    op.drop_index("ix_brand_datasets_brand", table_name="brand_datasets")
    op.drop_table("brand_datasets")
    op.drop_index("ix_brand_classes_brand", table_name="brand_classes")
    op.drop_table("brand_classes")
    op.drop_index("ix_brands_updated_at", table_name="brands")
    op.drop_table("brands")
