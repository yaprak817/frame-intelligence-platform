from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.jobs import JobStatus, OutboxEventType, SourceType
from app.models.job_outbox import JobOutbox
from app.models.processing_job import ProcessingJob
from app.repositories.jobs import (
    DuplicateIdempotencyKeyError,
    JobRepository,
)
from app.schemas.jobs import ProcessingConfigRequest, safe_url_display
from app.security.source_secrets import SourceSecretCipher

URL_IDEMPOTENCY_SCOPE = "POST:/api/v1/jobs/url"


class IdempotencyConflictError(RuntimeError):
    pass


class JobNotFoundError(RuntimeError):
    pass


class JobService:
    def __init__(
        self,
        repository: JobRepository,
        cipher: SourceSecretCipher,
    ) -> None:
        self._repository = repository
        self._cipher = cipher

    async def create_url_job(
        self,
        raw_url: str,
        processing: ProcessingConfigRequest,
        idempotency_key: str,
    ) -> tuple[ProcessingJob, bool]:
        config = processing.model_dump(mode="json")
        fingerprint = self._cipher.fingerprint(
            {"source_type": SourceType.URL, "url": raw_url, "processing": config}
        )
        existing = await self._repository.find_by_idempotency(
            URL_IDEMPOTENCY_SCOPE, idempotency_key
        )
        if existing is not None:
            return self._resolve_idempotent(existing, fingerprint), False

        job_id = uuid4()
        now = datetime.now(UTC)
        job = ProcessingJob(
            id=job_id,
            status=JobStatus.PENDING_DISPATCH,
            source_type=SourceType.URL,
            source_display=safe_url_display(raw_url),
            source_secret=self._cipher.encrypt(raw_url, job_id),
            processing_config=config,
            created_at=now,
            started_at=None,
            completed_at=None,
            failure_code=None,
            failure_message=None,
            attempt_count=0,
            idempotency_scope=URL_IDEMPOTENCY_SCOPE,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            result_reference=None,
            version=1,
        )
        event = JobOutbox(
            id=uuid4(),
            aggregate_id=job_id,
            event_type=OutboxEventType.PROCESS_VIDEO_JOB,
            payload={"job_id": str(job_id)},
            created_at=now,
            published_at=None,
            attempt_count=0,
        )
        try:
            await self._repository.create_with_outbox(job, event)
        except DuplicateIdempotencyKeyError:
            concurrent = await self._repository.find_by_idempotency(
                URL_IDEMPOTENCY_SCOPE, idempotency_key
            )
            if concurrent is None:
                raise
            return self._resolve_idempotent(concurrent, fingerprint), False
        return job, True

    async def get_job(self, job_id: UUID) -> ProcessingJob:
        job = await self._repository.get(job_id)
        if job is None:
            raise JobNotFoundError
        return job

    @staticmethod
    def _resolve_idempotent(existing: ProcessingJob, fingerprint: str) -> ProcessingJob:
        if not _constant_time_equal(existing.request_fingerprint, fingerprint):
            raise IdempotencyConflictError
        return existing


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
