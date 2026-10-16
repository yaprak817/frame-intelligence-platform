"""Add user ownership to top-level resources.

Revision ID: 20261012_0014
Revises: 20261011_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261012_0014"
down_revision: str | None = "20261011_0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "brands",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_brands_owner_user",
        "brands",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_brands_normalized_name",
        "brands",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_brands_owner_normalized_name",
        "brands",
        ["owner_user_id", "normalized_name"],
    )
    op.create_index(
        "ix_brands_owner_updated_at",
        "brands",
        ["owner_user_id", "updated_at"],
    )

    op.add_column(
        "processing_jobs",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_processing_jobs_owner_user",
        "processing_jobs",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_processing_jobs_owner_created_at",
        "processing_jobs",
        ["owner_user_id", "created_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()

    owned_brands = connection.execute(
        sa.text(
            "SELECT EXISTS("
            "SELECT 1 FROM brands WHERE owner_user_id IS NOT NULL"
            ")"
        )
    ).scalar()
    owned_jobs = connection.execute(
        sa.text(
            "SELECT EXISTS("
            "SELECT 1 FROM processing_jobs WHERE owner_user_id IS NOT NULL"
            ")"
        )
    ).scalar()

    if owned_brands or owned_jobs:
        raise RuntimeError(
            "Downgrade refused while user-owned brands or jobs exist"
        )

    op.drop_index(
        "ix_processing_jobs_owner_created_at",
        table_name="processing_jobs",
    )
    op.drop_constraint(
        "fk_processing_jobs_owner_user",
        "processing_jobs",
        type_="foreignkey",
    )
    op.drop_column("processing_jobs", "owner_user_id")

    op.drop_index(
        "ix_brands_owner_updated_at",
        table_name="brands",
    )
    op.drop_constraint(
        "uq_brands_owner_normalized_name",
        "brands",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_brands_normalized_name",
        "brands",
        ["normalized_name"],
    )
    op.drop_constraint(
        "fk_brands_owner_user",
        "brands",
        type_="foreignkey",
    )
    op.drop_column("brands", "owner_user_id")