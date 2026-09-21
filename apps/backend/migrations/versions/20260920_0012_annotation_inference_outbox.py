"""Persist annotation inference dispatches.

Revision ID: 20260920_0012
Revises: 20260916_0011
"""

import sqlalchemy as sa
from alembic import op

revision = "20260920_0012"
down_revision = "20260916_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "annotation_inference_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("inference_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["inference_id"], ["annotation_inference_runs.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("inference_id", name="uq_annotation_inference_outbox_run"),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_annotation_inference_outbox_attempt"
        ),
    )
    op.create_index(
        "ix_annotation_inference_outbox_ready",
        "annotation_inference_outbox",
        ["next_attempt_at", "created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.execute(
        "INSERT INTO annotation_inference_outbox "
        "(id, inference_id, created_at, published_at, attempt_count, next_attempt_at) "
        "SELECT id, id, created_at, NULL, 0, CURRENT_TIMESTAMP "
        "FROM annotation_inference_runs WHERE status = 'PENDING'"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM annotation_inference_outbox LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Cannot downgrade: annotation inference outbox is not empty")
    op.drop_index(
        "ix_annotation_inference_outbox_ready", table_name="annotation_inference_outbox"
    )
    op.drop_table("annotation_inference_outbox")
