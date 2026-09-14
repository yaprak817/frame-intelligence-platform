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
