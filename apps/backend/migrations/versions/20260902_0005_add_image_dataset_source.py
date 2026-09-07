"""allow image-dataset object-storage sources

Revision ID: 20260902_0005
Revises: 20260901_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0005"
down_revision: str | None = "20260901_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_CONSTRAINT = (
    "(source_type = 'URL' AND source_secret IS NOT NULL AND "
    "source_reference IS NULL) OR (source_type = 'UPLOAD' AND "
    "source_secret IS NULL AND source_reference IS NOT NULL)"
)
_NEW_CONSTRAINT = (
    f"({_OLD_CONSTRAINT}) OR (source_type = 'IMAGE_DATASET' AND "
    "source_secret IS NULL AND source_reference IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_processing_jobs_source_consistency", "processing_jobs", type_="check"
    )
    op.create_check_constraint(
        "ck_processing_jobs_source_consistency",
        "processing_jobs",
        _NEW_CONSTRAINT,
    )


def downgrade() -> None:
    dataset_rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM processing_jobs "
                "WHERE source_type = 'IMAGE_DATASET' LIMIT 1"
            )
        )
        .first()
    )
    if dataset_rows is not None:
        raise RuntimeError(
            "Downgrade refused while IMAGE_DATASET jobs exist; "
            "remove them explicitly first"
        )
    op.drop_constraint(
        "ck_processing_jobs_source_consistency", "processing_jobs", type_="check"
    )
    op.create_check_constraint(
        "ck_processing_jobs_source_consistency",
        "processing_jobs",
        _OLD_CONSTRAINT,
    )
