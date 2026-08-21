"""Add object-storage upload source references."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0002"
down_revision: str | None = "20260820_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("processing_jobs", "source_secret", nullable=True)
    op.add_column(
        "processing_jobs",
        sa.Column("source_reference", sa.JSON(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_processing_jobs_source_consistency",
        "processing_jobs",
        "(source_type = 'URL' AND source_secret IS NOT NULL AND "
        "source_reference IS NULL) OR (source_type = 'UPLOAD' AND "
        "source_secret IS NULL AND source_reference IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_processing_jobs_source_consistency", "processing_jobs", type_="check"
    )
    op.drop_column("processing_jobs", "source_reference")
    op.alter_column("processing_jobs", "source_secret", nullable=False)
