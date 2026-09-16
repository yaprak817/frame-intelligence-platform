from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnnotationTrainingRun(Base):
    __tablename__ = "annotation_training_runs"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_annotation_training_runs_project"
        ),
        UniqueConstraint(
            "project_id", "snapshot_version", name="uq_annotation_training_runs_version"
        ),
        UniqueConstraint(
            "project_id",
            "source_revision",
            name="uq_annotation_training_runs_source_revision",
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_annotation_training_runs_idempotency",
        ),
        UniqueConstraint(
            "project_id",
            "request_fingerprint",
            name="uq_annotation_training_runs_fingerprint",
        ),
        CheckConstraint(
            "snapshot_version > 0", name="ck_annotation_training_runs_version"
        ),
        CheckConstraint(
            "source_revision >= 0", name="ck_annotation_training_runs_revision"
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SNAPSHOT_READY','SUCCEEDED','FAILED')",
            name="ck_annotation_training_runs_status",
        ),
        CheckConstraint(
            "selected_image_count BETWEEN 50 AND 200",
            name="ck_annotation_training_runs_image_count",
        ),
        CheckConstraint(
            "selected_class_count BETWEEN 1 AND 20",
            name="ck_annotation_training_runs_class_count",
        ),
        CheckConstraint(
            "selected_box_count BETWEEN 1 AND 20000",
            name="ck_annotation_training_runs_box_count",
        ),
        CheckConstraint(
            "train_image_count > 0 AND validation_image_count > 0 "
            "AND train_image_count + validation_image_count = selected_image_count",
            name="ck_annotation_training_runs_split_count",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_annotation_training_runs_fingerprint",
        ),
        CheckConstraint(
            "config_hash ~ '^[0-9a-f]{64}$'",
            name="ck_annotation_training_runs_config_hash",
        ),
        CheckConstraint(
            "progress_total >= 0 AND progress_completed >= 0 "
            "AND progress_completed <= progress_total",
            name="ck_annotation_training_runs_progress",
        ),
        CheckConstraint(
            "attempt_generation >= 0", name="ck_annotation_training_runs_attempt"
        ),
        CheckConstraint(
            "(status = 'FAILED') = (failure_code IS NOT NULL)",
            name="ck_annotation_training_runs_failure",
        ),
        CheckConstraint(
            "snapshot_artifact_size_bytes IS NULL OR "
            "snapshot_artifact_size_bytes BETWEEN 1 AND 1073741824",
            name="ck_annotation_training_runs_snapshot_size",
        ),
        CheckConstraint(
            "model_artifact_size_bytes IS NULL OR "
            "model_artifact_size_bytes BETWEEN 1 AND 268435456",
            name="ck_annotation_training_runs_model_size",
        ),
        CheckConstraint(
            "snapshot_artifact_sha256 IS NULL OR "
            "snapshot_artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_annotation_training_runs_snapshot_sha",
        ),
        CheckConstraint(
            "model_artifact_sha256 IS NULL OR model_artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_annotation_training_runs_model_sha",
        ),
        CheckConstraint(
            "status <> 'SUCCEEDED' OR (model_version IS NOT NULL AND "
            "model_artifact_reference IS NOT NULL AND "
            "model_artifact_size_bytes IS NOT NULL AND "
            "model_artifact_sha256 IS NOT NULL AND "
            "snapshot_artifact_reference IS NOT NULL AND "
            "snapshot_artifact_size_bytes IS NOT NULL AND "
            "snapshot_artifact_sha256 IS NOT NULL)",
            name="ck_annotation_training_runs_success",
        ),
        UniqueConstraint(
            "project_id",
            "model_version",
            name="uq_annotation_training_runs_model_version",
        ),
        Index(
            "ix_annotation_training_runs_project_created", "project_id", "created_at"
        ),
        Index(
            "uq_annotation_training_runs_active_project",
            "project_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING','RUNNING')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("annotation_projects.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_image_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_class_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_box_count: Mapped[int] = mapped_column(Integer, nullable=False)
    train_image_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_image_count: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    snapshot_artifact_reference: Mapped[str | None] = mapped_column(Text)
    snapshot_artifact_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    snapshot_artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    model_artifact_reference: Mapped[str | None] = mapped_column(Text)
    model_artifact_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    model_artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    progress_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    working_prefix: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[int | None] = mapped_column(Integer)


class AnnotationTrainingOutbox(Base):
    __tablename__ = "annotation_training_outbox"
    __table_args__ = (
        UniqueConstraint(
            "training_id", "event_type", name="uq_annotation_training_outbox_event"
        ),
        CheckConstraint(
            "event_type = 'START_ANNOTATION_TRAINING'",
            name="ck_annotation_training_outbox_event",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_annotation_training_outbox_attempt"
        ),
        Index(
            "ix_annotation_training_outbox_ready",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    training_id: Mapped[UUID] = mapped_column(
        ForeignKey("annotation_training_runs.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AnnotationTrainingSnapshotClass(Base):
    __tablename__ = "annotation_training_snapshot_classes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["training_id", "project_id"],
            ["annotation_training_runs.id", "annotation_training_runs.project_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "training_id", "class_id", name="uq_annotation_training_snapshot_classes_id"
        ),
        CheckConstraint(
            "yolo_index >= 0", name="ck_annotation_training_snapshot_classes_yolo"
        ),
    )
    training_id: Mapped[UUID] = mapped_column(primary_key=True)
    yolo_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    class_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)


class AnnotationTrainingSnapshotImage(Base):
    __tablename__ = "annotation_training_snapshot_images"
    __table_args__ = (
        ForeignKeyConstraint(
            ["training_id", "project_id"],
            ["annotation_training_runs.id", "annotation_training_runs.project_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "image_index >= 0", name="ck_annotation_training_snapshot_images_index"
        ),
        CheckConstraint(
            "split IN ('train','val')",
            name="ck_annotation_training_snapshot_images_split",
        ),
        CheckConstraint(
            "width > 0 AND height > 0",
            name="ck_annotation_training_snapshot_images_dimensions",
        ),
        CheckConstraint(
            "source_size_bytes > 0", name="ck_annotation_training_snapshot_images_size"
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_annotation_training_snapshot_images_sha256",
        ),
    )
    training_id: Mapped[UUID] = mapped_column(primary_key=True)
    image_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_ms: Mapped[int | None] = mapped_column(BigInteger)
    split: Mapped[str] = mapped_column(String(5), nullable=False)


class AnnotationTrainingSnapshotBox(Base):
    __tablename__ = "annotation_training_snapshot_boxes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["training_id", "project_id"],
            ["annotation_training_runs.id", "annotation_training_runs.project_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["training_id", "image_index"],
            [
                "annotation_training_snapshot_images.training_id",
                "annotation_training_snapshot_images.image_index",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["training_id", "yolo_index"],
            [
                "annotation_training_snapshot_classes.training_id",
                "annotation_training_snapshot_classes.yolo_index",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "x_center >= 0 AND x_center <= 1",
            name="ck_annotation_training_snapshot_boxes_x",
        ),
        CheckConstraint(
            "y_center >= 0 AND y_center <= 1",
            name="ck_annotation_training_snapshot_boxes_y",
        ),
        CheckConstraint(
            "width > 0 AND width <= 1",
            name="ck_annotation_training_snapshot_boxes_width",
        ),
        CheckConstraint(
            "height > 0 AND height <= 1",
            name="ck_annotation_training_snapshot_boxes_height",
        ),
        CheckConstraint(
            "x_center - width / 2 >= 0 AND x_center + width / 2 <= 1",
            name="ck_annotation_training_snapshot_boxes_x_bounds",
        ),
        CheckConstraint(
            "y_center - height / 2 >= 0 AND y_center + height / 2 <= 1",
            name="ck_annotation_training_snapshot_boxes_y_bounds",
        ),
    )
    training_id: Mapped[UUID] = mapped_column(primary_key=True)
    box_id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    image_index: Mapped[int] = mapped_column(Integer, nullable=False)
    yolo_index: Mapped[int] = mapped_column(Integer, nullable=False)
    x_center: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    y_center: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    width: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    height: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
