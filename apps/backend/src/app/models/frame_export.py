from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FrameExport(Base):
    __tablename__ = "frame_exports"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "result_run_token",
            "mode",
            "selection_hash",
            name="uq_frame_exports_selection",
        ),
        Index("ix_frame_exports_job_created", "job_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False
    )
    result_run_token: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    frame_indices: Mapped[list[int] | None] = mapped_column(JSON(none_as_null=True))
    selection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    artifact_reference: Mapped[str | None] = mapped_column(Text)
    artifact_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_generation: Mapped[int] = mapped_column(nullable=False, default=0)
    working_object_key: Mapped[str | None] = mapped_column(Text)


class FrameExportOutbox(Base):
    __tablename__ = "frame_export_outbox"
    __table_args__ = (
        Index(
            "ix_frame_export_outbox_ready",
            "published_at",
            "next_attempt_at",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    export_id: Mapped[UUID] = mapped_column(
        ForeignKey("frame_exports.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
