from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnnotationInferenceRun(Base):
    __tablename__ = "annotation_inference_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["training_id", "project_id"],
            ["annotation_training_runs.id", "annotation_training_runs.project_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "model_version > 0", name="ck_annotation_inference_runs_model_version"
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')",
            name="ck_annotation_inference_runs_status",
        ),
        CheckConstraint(
            "target_image_count > 0",
            name="ck_annotation_inference_runs_target_count",
        ),
        CheckConstraint(
            "processed_image_count >= 0 AND "
            "processed_image_count <= target_image_count",
            name="ck_annotation_inference_runs_progress",
        ),
        CheckConstraint(
            "created_box_count >= 0",
            name="ck_annotation_inference_runs_box_count",
        ),
        CheckConstraint(
            "(status = 'FAILED') = (failure_code IS NOT NULL)",
            name="ck_annotation_inference_runs_failure",
        ),
        Index(
            "ix_annotation_inference_runs_project_created", "project_id", "created_at"
        ),
        Index(
            "uq_annotation_inference_runs_active_project",
            "project_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING','RUNNING')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("annotation_projects.id", ondelete="CASCADE"), nullable=False
    )
    training_id: Mapped[UUID] = mapped_column(nullable=False)
    model_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    target_image_count: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_image_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    created_box_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))


class AnnotationInferenceOutbox(Base):
    __tablename__ = "annotation_inference_outbox"
    __table_args__ = (
        UniqueConstraint("inference_id", name="uq_annotation_inference_outbox_run"),
        CheckConstraint(
            "attempt_count >= 0", name="ck_annotation_inference_outbox_attempt"
        ),
        Index(
            "ix_annotation_inference_outbox_ready",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    inference_id: Mapped[UUID] = mapped_column(
        ForeignKey("annotation_inference_runs.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
