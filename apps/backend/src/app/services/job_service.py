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
from app.storage.s3 import (
    ObjectStorageError,
    ObjectStorageUploader,
    S3ObjectReference,
    UploadTooLargeError,
)

URL_IDEMPOTENCY_SCOPE = "POST:/api/v1/jobs/url"
UPLOAD_IDEMPOTENCY_SCOPE = "POST:/api/v1/jobs/upload"
IMAGE_DATASET_IDEMPOTENCY_SCOPE = "POST:/api/v1/jobs/image-dataset"
_ALLOWED_UPLOADS = {
    ".avi": {"video/x-msvideo"},
    ".m4v": {"video/x-m4v", "video/mp4"},
    ".mkv": {"video/x-matroska"},
    ".mov": {"video/quicktime"},
    ".mp4": {"video/mp4"},
    ".webm": {"video/webm"},
}
_ALLOWED_IMAGES = {
    ".jpg": {"image/jpeg", "application/octet-stream"},
    ".jpeg": {"image/jpeg", "application/octet-stream"},
    ".png": {"image/png", "application/octet-stream"},
    ".webp": {"image/webp", "application/octet-stream"},
}
_ALLOWED_ARCHIVES = {
    ".zip": {
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    }
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
        *,
        image_max_files: int = 1000,
        image_max_file_bytes: int = 50 * 1024 * 1024,
        image_max_total_bytes: int = 2 * 1024 * 1024 * 1024,
        image_zip_max_compressed_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self._repository = repository
        self._cipher = cipher
        self._storage = storage
        self._image_max_files = image_max_files
        self._image_max_file_bytes = image_max_file_bytes
        self._image_max_total_bytes = image_max_total_bytes
        self._image_zip_max_compressed_bytes = image_zip_max_compressed_bytes

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

    async def create_image_dataset_job(
        self,
        files: list[object],
        filenames: list[str],
        content_types: list[str],
        file_sizes: list[int | None],
        *,
        archive: bool,
        idempotency_key: str,
    ) -> tuple[ProcessingJob, bool]:
        if self._storage is None:
            raise RuntimeError("Object storage is not configured")
        if (
            not files
            or len(files) != len(filenames)
            or len(files) != len(content_types)
        ):
            raise UnsupportedUploadError("Image dataset selection is invalid")
        if archive and len(files) != 1:
            raise UnsupportedUploadError("Select one ZIP archive")
        if not archive and len(files) > self._image_max_files:
            raise UnsupportedUploadError("Image count exceeds the configured limit")
        normalized: list[tuple[str, str]] = []
        total = 0
        allowed = _ALLOWED_ARCHIVES if archive else _ALLOWED_IMAGES
        for name, content_type, size in zip(
            filenames, content_types, file_sizes, strict=True
        ):
            safe_name, suffix = normalize_dataset_filename(name, content_type, allowed)
            if size is not None:
                limit = (
                    self._image_zip_max_compressed_bytes
                    if archive
                    else self._image_max_file_bytes
                )
                if size <= 0 or size > limit:
                    raise UnsupportedUploadError("Dataset file size is invalid")
                total += size
            normalized.append((safe_name, suffix))
        if total > self._image_max_total_bytes:
            raise UnsupportedUploadError("Dataset upload exceeds the configured limit")

        existing = await self._repository.find_by_idempotency(
            IMAGE_DATASET_IDEMPOTENCY_SCOPE, idempotency_key
        )
        job_id = uuid4()
        references: list[S3ObjectReference] = []
        try:
            for index, (file, (_safe_name, suffix)) in enumerate(
                zip(files, normalized, strict=True)
            ):
                object_key = (
                    f"jobs/{job_id}/source/archive.zip"
                    if archive
                    else f"jobs/{job_id}/source/images/{index:06d}{suffix}"
                )
                limit = (
                    self._image_zip_max_compressed_bytes
                    if archive
                    else self._image_max_file_bytes
                )
                reference = await self._storage.upload(
                    _CountingUpload(file, limit), object_key
                )  # type: ignore[arg-type]
                references.append(reference)
                if reference.size_bytes > limit:
                    raise UnsupportedUploadError("Dataset file size is invalid")
            if (
                sum(item.size_bytes for item in references)
                > self._image_max_total_bytes
            ):
                raise UnsupportedUploadError(
                    "Dataset upload exceeds the configured limit"
                )
        except Exception:
            await self._compensate_dataset(references, job_id)
            raise

        fingerprint = self._cipher.fingerprint(
            {
                "source_type": SourceType.IMAGE_DATASET,
                "archive": archive,
                "files": [
                    {"name": name, "size": ref.size_bytes, "sha256": ref.sha256}
                    for (name, _suffix), ref in zip(normalized, references, strict=True)
                ],
            }
        )
        if existing is not None:
            await self._compensate_dataset(references, job_id)
            return self._resolve_idempotent(existing, fingerprint), False
        now = datetime.now(UTC)
        job = ProcessingJob(
            id=job_id,
            status=JobStatus.PENDING_DISPATCH,
            source_type=SourceType.IMAGE_DATASET,
            source_display=(
                "ZIP görsel veri seti" if archive else f"{len(files)} görsel"
            ),
            source_secret=None,
            source_reference={
                "schema_version": 1,
                "kind": "zip" if archive else "images",
                "items": [
                    {**ref.to_dict(), "safe_name": name}
                    for (name, _suffix), ref in zip(normalized, references, strict=True)
                ],
            },
            processing_config={"dataset_schema_version": 1},
            created_at=now,
            started_at=None,
            completed_at=None,
            failure_code=None,
            failure_message=None,
            attempt_count=0,
            idempotency_scope=IMAGE_DATASET_IDEMPOTENCY_SCOPE,
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
            event_type=OutboxEventType.PROCESS_IMAGE_DATASET_JOB,
            payload={"job_id": str(job_id)},
            created_at=now,
            published_at=None,
            attempt_count=0,
            next_attempt_at=now,
        )
        try:
            await self._repository.create_with_outbox(job, event)
        except DuplicateIdempotencyKeyError:
            await self._compensate_dataset(references, job_id)
            concurrent = await self._repository.find_by_idempotency(
                IMAGE_DATASET_IDEMPOTENCY_SCOPE, idempotency_key
            )
            if concurrent is None:
                raise
            return self._resolve_idempotent(concurrent, fingerprint), False
        except Exception:
            await self._compensate_dataset(references, job_id)
            raise
        return job, True

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

    async def _compensate_dataset(
        self, references: list[S3ObjectReference], job_id: UUID
    ) -> None:
        failed = False
        for reference in references:
            try:
                assert self._storage is not None
                await self._storage.delete(reference)
            except Exception:
                failed = True
                logger.exception("Dataset object cleanup failed for job %s", job_id)
        if failed:
            raise ObjectStorageError("Dataset cleanup requires recovery")


class _CountingUpload:
    def __init__(self, source: object, maximum: int) -> None:
        self._source = source
        self._maximum = maximum
        self._consumed = 0
        self.content_type = getattr(source, "content_type", None)

    async def read(self, size: int = -1) -> bytes:
        chunk = await self._source.read(size)  # type: ignore[attr-defined]
        self._consumed += len(chunk)
        if self._consumed > self._maximum:
            raise UploadTooLargeError
        return chunk


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


def normalize_dataset_filename(
    filename: str, content_type: str, allowed: dict[str, set[str]]
) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFC", filename)
    normalized = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    normalized = re.sub(r"[\x00-\x1f\x7f]", "", normalized).strip()[:255]
    suffix = PurePath(normalized).suffix.lower()
    if suffix not in allowed or content_type.lower() not in allowed[suffix]:
        raise UnsupportedUploadError("Unsupported image dataset upload type")
    return normalized or f"dataset{suffix}", suffix


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
