from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnnotationProject(Base):
    __tablename__ = "annotation_projects"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "result_run_token", name="uq_annotation_projects_job_run"
        ),
        CheckConstraint("revision >= 0", name="ck_annotation_projects_revision"),
        Index("ix_annotation_projects_job", "job_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False
    )
    result_run_token: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AnnotationClass(Base):
    __tablename__ = "annotation_classes"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_annotation_classes_project_id"),
        UniqueConstraint(
            "project_id", "yolo_index", name="uq_annotation_classes_yolo_index"
        ),
        UniqueConstraint(
            "project_id",
            "normalized_name",
            name="uq_annotation_classes_normalized_name",
        ),
        CheckConstraint("yolo_index >= 0", name="ck_annotation_classes_yolo_index"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("annotation_projects.id", ondelete="CASCADE"), nullable=False
    )
    yolo_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(80), nullable=False)
    color: Mapped[str] = mapped_column(CHAR(7), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AnnotationImage(Base):
    __tablename__ = "annotation_images"
    __table_args__ = (
        CheckConstraint("image_index >= 0", name="ck_annotation_images_index"),
        CheckConstraint(
            "image_sha256 ~ '^[0-9a-f]{64}$'", name="ck_annotation_images_sha256"
        ),
        CheckConstraint(
            "yolo_sha256 ~ '^[0-9a-f]{64}$'", name="ck_annotation_images_yolo_sha256"
        ),
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("annotation_projects.id", ondelete="CASCADE"), primary_key=True
    )
    image_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    yolo_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AnnotationBox(Base):
    __tablename__ = "annotation_boxes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "image_index"],
            ["annotation_images.project_id", "annotation_images.image_index"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "class_id"],
            ["annotation_classes.project_id", "annotation_classes.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("project_id", "id", name="uq_annotation_boxes_project_id"),
        CheckConstraint(
            "x_center >= 0 AND x_center <= 1", name="ck_annotation_boxes_x_center"
        ),
        CheckConstraint(
            "y_center >= 0 AND y_center <= 1", name="ck_annotation_boxes_y_center"
        ),
        CheckConstraint("width > 0 AND width <= 1", name="ck_annotation_boxes_width"),
        CheckConstraint(
            "height > 0 AND height <= 1", name="ck_annotation_boxes_height"
        ),
        CheckConstraint(
            "x_center - width / 2 >= 0 AND x_center + width / 2 <= 1",
            name="ck_annotation_boxes_x_bounds",
        ),
        CheckConstraint(
            "y_center - height / 2 >= 0 AND y_center + height / 2 <= 1",
            name="ck_annotation_boxes_y_bounds",
        ),
        Index("ix_annotation_boxes_project_image", "project_id", "image_index"),
        Index("ix_annotation_boxes_project_class", "project_id", "class_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False)
    image_index: Mapped[int] = mapped_column(Integer, nullable=False)
    class_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    x_center: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    y_center: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    width: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    height: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
