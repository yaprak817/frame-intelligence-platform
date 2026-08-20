from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job_outbox import JobOutbox
from app.models.processing_job import ProcessingJob


class DuplicateIdempotencyKeyError(RuntimeError):
    """Raised when another transaction already claimed an idempotency key."""


class JobRepository(Protocol):
    async def get(self, job_id: UUID) -> ProcessingJob | None: ...

    async def find_by_idempotency(
        self, scope: str, key: str
    ) -> ProcessingJob | None: ...

    async def create_with_outbox(
        self, job: ProcessingJob, event: JobOutbox
    ) -> None: ...


class SQLAlchemyJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: UUID) -> ProcessingJob | None:
        return await self._session.get(ProcessingJob, job_id)

    async def find_by_idempotency(self, scope: str, key: str) -> ProcessingJob | None:
        result = await self._session.execute(
            select(ProcessingJob).where(
                ProcessingJob.idempotency_scope == scope,
                ProcessingJob.idempotency_key == key,
            )
        )
        return result.scalar_one_or_none()

    async def create_with_outbox(self, job: ProcessingJob, event: JobOutbox) -> None:
        try:
            self._session.add(job)
            await self._session.flush()
            self._session.add(event)
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            original = error.orig
            diagnostic = getattr(original, "diag", None)
            if (
                getattr(diagnostic, "constraint_name", None)
                == "uq_processing_jobs_idempotency"
            ):
                raise DuplicateIdempotencyKeyError from error
            raise
        except Exception:
            await self._session.rollback()
            raise
