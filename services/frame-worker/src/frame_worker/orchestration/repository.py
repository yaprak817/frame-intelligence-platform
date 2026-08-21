from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    or_,
    select,
    update,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.engine import Engine, RowMapping

from frame_worker.orchestration.contracts import (
    ClaimResult,
    JobRecord,
    JobStatus,
)

metadata = MetaData()
processing_jobs = Table(
    "processing_jobs",
    metadata,
    Column("id", SQLUuid, primary_key=True),
    Column("status", String(32), nullable=False),
    Column("source_type", String(16), nullable=False),
    Column("source_secret", Text),
    Column("source_reference", JSON),
    Column("processing_config", JSON, nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("failure_code", String(64)),
    Column("failure_message", Text),
    Column("attempt_count", Integer, nullable=False),
    Column("result_reference", Text),
    Column("result_summary", JSON),
    Column("run_token", SQLUuid),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("version", Integer, nullable=False),
)


class JobRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> "JobRepository":
        return cls(create_engine(database_url, pool_pre_ping=True))

    def close(self) -> None:
        self._engine.dispose()

    def get(self, job_id: UUID) -> JobRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(*_record_columns()).where(processing_jobs.c.id == job_id)
                )
                .mappings()
                .one_or_none()
            )
        return _record(row) if row else None

    def claim(self, job_id: UUID, lease_seconds: float) -> ClaimResult:
        now = datetime.now(UTC)
        token = uuid4()
        with self._engine.begin() as connection:
            current = (
                connection.execute(
                    select(
                        processing_jobs.c.status,
                        processing_jobs.c.version,
                        processing_jobs.c.lease_expires_at,
                    ).where(processing_jobs.c.id == job_id)
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                return ClaimResult(None)
            if current["status"] == JobStatus.PENDING_DISPATCH:
                return ClaimResult(None, retry_later=True)
            normal = current["status"] == JobStatus.QUEUED
            stale = (
                current["status"] == JobStatus.RUNNING
                and current["lease_expires_at"] is not None
                and _utc(current["lease_expires_at"]) <= now
            )
            if not normal and not stale:
                return ClaimResult(None)
            statement = (
                update(processing_jobs)
                .where(
                    processing_jobs.c.id == job_id,
                    processing_jobs.c.version == current["version"],
                    or_(
                        processing_jobs.c.status == JobStatus.QUEUED,
                        and_(
                            processing_jobs.c.status == JobStatus.RUNNING,
                            processing_jobs.c.lease_expires_at <= now,
                        ),
                    ),
                )
                .values(
                    status=JobStatus.RUNNING,
                    attempt_count=processing_jobs.c.attempt_count + 1,
                    run_token=token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    started_at=or_started_at(now),
                    completed_at=None,
                    failure_code=None,
                    failure_message=None,
                    version=processing_jobs.c.version + 1,
                )
                .returning(*_record_columns())
            )
            row = connection.execute(statement).mappings().one_or_none()
            return ClaimResult(_record(row) if row else None)

    def heartbeat(self, job_id: UUID, run_token: UUID, lease_seconds: float) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(processing_jobs)
                .where(
                    processing_jobs.c.id == job_id,
                    processing_jobs.c.status == JobStatus.RUNNING,
                    processing_jobs.c.run_token == run_token,
                )
                .values(
                    lease_expires_at=datetime.now(UTC)
                    + timedelta(seconds=lease_seconds)
                )
            )
            return result.rowcount == 1

    def release_for_retry(self, job_id: UUID, run_token: UUID) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(processing_jobs)
                .where(
                    processing_jobs.c.id == job_id,
                    processing_jobs.c.status == JobStatus.RUNNING,
                    processing_jobs.c.run_token == run_token,
                )
                .values(
                    status=JobStatus.QUEUED,
                    run_token=None,
                    lease_expires_at=None,
                    version=processing_jobs.c.version + 1,
                )
            )
            return result.rowcount == 1

    def succeed(self, job_id: UUID, run_token: UUID, summary: dict[str, Any]) -> bool:
        return self._finish(
            job_id,
            run_token,
            status=JobStatus.SUCCEEDED,
            result_summary=summary,
            failure_code=None,
            failure_message=None,
        )

    def fail(self, job_id: UUID, run_token: UUID, code: str, message: str) -> bool:
        return self._finish(
            job_id,
            run_token,
            status=JobStatus.FAILED,
            result_summary=None,
            failure_code=code,
            failure_message=message,
        )

    def fail_queued(self, job_id: UUID, code: str, message: str) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(processing_jobs)
                .where(
                    processing_jobs.c.id == job_id,
                    processing_jobs.c.status == JobStatus.QUEUED,
                    processing_jobs.c.run_token.is_(None),
                )
                .values(
                    status=JobStatus.FAILED,
                    completed_at=datetime.now(UTC),
                    failure_code=code,
                    failure_message=message,
                    lease_expires_at=None,
                    version=processing_jobs.c.version + 1,
                )
            )
            return result.rowcount == 1

    def _finish(self, job_id: UUID, run_token: UUID, **values: Any) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(processing_jobs)
                .where(
                    processing_jobs.c.id == job_id,
                    processing_jobs.c.status == JobStatus.RUNNING,
                    processing_jobs.c.run_token == run_token,
                )
                .values(
                    **values,
                    completed_at=datetime.now(UTC),
                    run_token=None,
                    lease_expires_at=None,
                    version=processing_jobs.c.version + 1,
                )
            )
            return result.rowcount == 1


def or_started_at(now: datetime):
    from sqlalchemy import func

    return func.coalesce(processing_jobs.c.started_at, now)


def _record_columns() -> tuple[Column[Any], ...]:
    return (
        processing_jobs.c.id,
        processing_jobs.c.status,
        processing_jobs.c.source_type,
        processing_jobs.c.source_secret,
        processing_jobs.c.source_reference,
        processing_jobs.c.processing_config,
        processing_jobs.c.attempt_count,
        processing_jobs.c.run_token,
        processing_jobs.c.lease_expires_at,
        processing_jobs.c.result_reference,
        processing_jobs.c.result_summary,
        processing_jobs.c.version,
    )


def _record(row: RowMapping) -> JobRecord:
    return JobRecord(**dict(row))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
