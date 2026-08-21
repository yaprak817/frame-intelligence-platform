import logging
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import PurePath
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
from app.storage.s3 import ObjectStorageUploader, S3ObjectReference

URL_IDEMPOTENCY_SCOPE = "POST:/api/v1/jobs/url"
UPLOAD_IDEMPOTENCY_SCOPE = "POST:/api/v1/jobs/upload"
_ALLOWED_UPLOADS = {
    ".avi": {"video/x-msvideo"},
    ".m4v": {"video/x-m4v", "video/mp4"},
    ".mkv": {"video/x-matroska"},
    ".mov": {"video/quicktime"},
    ".mp4": {"video/mp4"},
    ".webm": {"video/webm"},
}
logger = logging.getLogger(__name__)


class IdempotencyConflictError(RuntimeError):
    pass


class JobNotFoundError(RuntimeError):
    pass


class UnsupportedUploadError(RuntimeError):
    pass


class JobService:
    def __init__(
        self,
        repository: JobRepository,
        cipher: SourceSecretCipher,
        storage: ObjectStorageUploader | None = None,
    ) -> None:
        self._repository = repository
        self._cipher = cipher
        self._storage = storage

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
            source_reference=None,
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
            result_summary=None,
            run_token=None,
            lease_expires_at=None,
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
            next_attempt_at=now,
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

    async def create_upload_job(
        self,
        file: object,
        filename: str,
        content_type: str,
        file_size: int | None,
        processing: ProcessingConfigRequest,
        idempotency_key: str,
    ) -> tuple[ProcessingJob, bool]:
        if self._storage is None:
            raise RuntimeError("Object storage is not configured")
        safe_name, suffix = normalize_upload_filename(filename, content_type)
        config = processing.model_dump(mode="json")
        fingerprint = self._cipher.fingerprint(
            {
                "source_type": SourceType.UPLOAD,
                "filename": safe_name,
                "content_type": content_type.lower(),
                "file_size": file_size,
                "processing": config,
            }
        )
        existing = await self._repository.find_by_idempotency(
            UPLOAD_IDEMPOTENCY_SCOPE, idempotency_key
        )
        if existing is not None:
            return self._resolve_idempotent(existing, fingerprint), False

        job_id = uuid4()
        object_key = f"jobs/{job_id}/source/original{suffix}"
        reference = await self._storage.upload(file, object_key)  # type: ignore[arg-type]
        now = datetime.now(UTC)
        job = ProcessingJob(
            id=job_id,
            status=JobStatus.PENDING_DISPATCH,
            source_type=SourceType.UPLOAD,
            source_display=safe_name,
            source_secret=None,
            source_reference=reference.to_dict(),
            processing_config=config,
            created_at=now,
            started_at=None,
            completed_at=None,
            failure_code=None,
            failure_message=None,
            attempt_count=0,
            idempotency_scope=UPLOAD_IDEMPOTENCY_SCOPE,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            result_reference=None,
            result_summary=None,
            run_token=None,
            lease_expires_at=None,
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
            next_attempt_at=now,
        )
        try:
            await self._repository.create_with_outbox(job, event)
        except DuplicateIdempotencyKeyError:
            await self._compensate(reference, job_id)
            concurrent = await self._repository.find_by_idempotency(
                UPLOAD_IDEMPOTENCY_SCOPE, idempotency_key
            )
            if concurrent is None:
                raise
            return self._resolve_idempotent(concurrent, fingerprint), False
        except Exception:
            await self._compensate(reference, job_id)
            raise
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

    async def _compensate(self, reference: S3ObjectReference, job_id: UUID) -> None:
        assert self._storage is not None
        try:
            await self._storage.delete(reference)
        except Exception:
            logger.exception("Object cleanup failed for job %s", job_id)


def normalize_upload_filename(filename: str, content_type: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFC", filename)
    normalized = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    normalized = re.sub(r"[\x00-\x1f\x7f]", "", normalized).strip()
    normalized = normalized[:255]
    suffix = PurePath(normalized).suffix.lower()
    if (
        suffix not in _ALLOWED_UPLOADS
        or content_type.lower() not in _ALLOWED_UPLOADS[suffix]
    ):
        raise UnsupportedUploadError("Unsupported video upload type")
    return normalized or f"video{suffix}", suffix


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
