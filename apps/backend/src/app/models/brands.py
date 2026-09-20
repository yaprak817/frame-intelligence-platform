from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Brand(Base):
    __tablename__ = "brands"
    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_brands_normalized_name"),
        Index("ix_brands_updated_at", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BrandClass(Base):
    __tablename__ = "brand_classes"
    __table_args__ = (
        UniqueConstraint(
            "brand_id", "normalized_name", name="uq_brand_classes_brand_normalized_name"
        ),
        Index("ix_brand_classes_brand", "brand_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    brand_id: Mapped[UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(80), nullable=False)
    color: Mapped[str] = mapped_column(CHAR(7), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BrandDataset(Base):
    __tablename__ = "brand_datasets"
    __table_args__ = (
        UniqueConstraint(
            "annotation_project_id", name="uq_brand_datasets_annotation_project"
        ),
        UniqueConstraint("job_id", name="uq_brand_datasets_job"),
        Index("ix_brand_datasets_brand", "brand_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    brand_id: Mapped[UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True
    )
    annotation_project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("annotation_projects.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
