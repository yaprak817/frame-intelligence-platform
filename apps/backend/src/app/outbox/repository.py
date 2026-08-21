import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.jobs import JobStatus, OutboxEventType
from app.models.job_outbox import JobOutbox
from app.models.processing_job import ProcessingJob
from app.outbox.celery_client import JobMessagePublisher

logger = logging.getLogger(__name__)


class InvalidOutboxPayloadError(ValueError):
    pass


def parse_job_id(payload: object) -> UUID:
    if not isinstance(payload, dict) or set(payload) != {"job_id"}:
        raise InvalidOutboxPayloadError("Outbox payload must contain only job_id")
    try:
        return UUID(str(payload["job_id"]))
    except (TypeError, ValueError) as error:
        raise InvalidOutboxPayloadError("Outbox job_id must be a UUID") from error


class OutboxRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        random_source: random.Random | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._random = random_source or random.Random()

    async def publish_ready(
        self, publisher: JobMessagePublisher, batch_size: int
    ) -> int:
        processed = 0
        for _ in range(batch_size):
            if not await self._publish_one(publisher):
                break
            processed += 1
        return processed

    async def _publish_one(self, publisher: JobMessagePublisher) -> bool:
        async with self._session_factory() as session, session.begin():
            now = datetime.now(UTC)
            result = await session.execute(
                select(JobOutbox)
                .where(
                    JobOutbox.published_at.is_(None),
                    JobOutbox.next_attempt_at <= now,
                )
                .order_by(JobOutbox.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            event = result.scalar_one_or_none()
            if event is None:
                return False
            try:
                if event.event_type != OutboxEventType.PROCESS_VIDEO_JOB:
                    raise InvalidOutboxPayloadError("Unsupported outbox event type")
                job_id = parse_job_id(event.payload)
                if job_id != event.aggregate_id:
                    raise InvalidOutboxPayloadError("Outbox aggregate mismatch")
                await asyncio.to_thread(publisher.publish, job_id)
            except Exception as error:
                event.attempt_count += 1
                event.next_attempt_at = now + timedelta(
                    seconds=self._backoff_seconds(event.attempt_count)
                )
                logger.warning(
                    "Outbox publish deferred event_id=%s job_id=%s "
                    "attempt=%s error_type=%s",
                    event.id,
                    event.aggregate_id,
                    event.attempt_count,
                    type(error).__name__,
                )
                return True

            event.published_at = now
            event.attempt_count += 1
            await session.execute(
                update(ProcessingJob)
                .where(
                    ProcessingJob.id == job_id,
                    ProcessingJob.status == JobStatus.PENDING_DISPATCH,
                )
                .values(status=JobStatus.QUEUED, version=ProcessingJob.version + 1)
            )
            logger.info(
                "Outbox published event_id=%s job_id=%s attempt=%s",
                event.id,
                job_id,
                event.attempt_count,
            )
            return True

    def _backoff_seconds(self, attempt: int) -> float:
        ceiling = min(self._backoff_max, self._backoff_base * (2 ** (attempt - 1)))
        return self._random.uniform(ceiling / 2, ceiling)
