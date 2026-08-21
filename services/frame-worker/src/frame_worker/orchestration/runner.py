import base64
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from frame_worker.ingestion.adapters.direct_http import DirectHTTPVideoAdapter
from frame_worker.ingestion.adapters.yt_dlp import YtDlpURLAdapter
from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.object_storage import (
    ObjectStorageDownloader,
    ObjectStorageVideoSource,
    S3ObjectReference,
)
from frame_worker.ingestion.resolver import SourceResolver
from frame_worker.ingestion.service import SourceProcessingService
from frame_worker.ingestion.url import GenericURLVideoSource
from frame_worker.orchestration.config import WorkerSettings, decode_encryption_key
from frame_worker.orchestration.contracts import JobRecord, SourceType
from frame_worker.orchestration.failures import Failure, classify_failure
from frame_worker.orchestration.heartbeat import LeaseHeartbeat
from frame_worker.orchestration.repository import JobRepository
from frame_worker.processing.pipeline import (
    ProcessingConfig,
    ProcessingSummary,
    VideoProcessor,
)

logger = logging.getLogger(__name__)
_PROCESSING_FIELDS = {"candidate_fps", "selection_window_seconds"}
_REFERENCE_FIELDS = {
    "schema_version",
    "bucket",
    "object_key",
    "version_id",
    "etag",
    "size_bytes",
    "sha256",
}


class RetryableExecutionError(RuntimeError):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.code)
        self.failure = failure


class RetryLaterError(RuntimeError):
    pass


class OwnershipLostError(RuntimeError):
    pass


class JobRunner:
    def __init__(
        self,
        settings: WorkerSettings,
        repository: JobRepository,
        repository_factory: Callable[[], JobRepository],
        processor_factory: Callable[[ProcessingConfig], VideoProcessor] = (
            VideoProcessor
        ),
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.repository_factory = repository_factory
        self.processor_factory = processor_factory

    def execute(self, job_id: UUID) -> bool:
        claim = self.repository.claim(job_id, self.settings.lease_seconds)
        if claim.job is None:
            if claim.retry_later:
                raise RetryLaterError
            return False
        job = claim.job
        assert job.run_token is not None
        try:
            config = processing_config(job.processing_config)
            source = self._source(job)
            processor = self.processor_factory(config)
            with LeaseHeartbeat(
                self.repository_factory,
                job.id,
                job.run_token,
                self.settings.lease_seconds,
                self.settings.heartbeat_interval_seconds,
            ) as heartbeat:
                with tempfile.TemporaryDirectory(
                    prefix=f"frame-job-{job.id}-",
                    dir=self.settings.processing_temp_root,
                ) as workspace:
                    summary = SourceProcessingService(processor).process(
                        source, Path(workspace) / "frames"
                    )
                if heartbeat.ownership_lost:
                    raise OwnershipLostError
            if not self.repository.succeed(
                job.id, job.run_token, result_summary(summary)
            ):
                raise OwnershipLostError
            logger.info(
                "Job transition job_id=%s attempt=%s transition=RUNNING_TO_SUCCEEDED",
                job.id,
                job.attempt_count,
            )
            return True
        except OwnershipLostError:
            logger.warning("Job completion rejected job_id=%s", job.id)
            return False
        except Exception as error:
            failure = classify_failure(error, job.source_type)
            self._handle_failure(job, failure)
            if failure.retryable:
                raise RetryableExecutionError(failure) from error
            return False

    def _handle_failure(self, job: JobRecord, failure: Failure) -> None:
        assert job.run_token is not None
        if failure.retryable:
            released = self.repository.release_for_retry(job.id, job.run_token)
            if not released:
                raise OwnershipLostError
            logger.warning(
                "Job released for retry job_id=%s attempt=%s code=%s",
                job.id,
                job.attempt_count,
                failure.code,
            )
            return
        self.repository.fail(job.id, job.run_token, failure.code, failure.message)
        logger.warning(
            "Job failed job_id=%s attempt=%s code=%s",
            job.id,
            job.attempt_count,
            failure.code,
        )

    def _source(self, job: JobRecord):
        ingestion_config = IngestionConfig(
            max_download_bytes=self.settings.max_download_bytes
        )
        if job.source_type == SourceType.URL:
            if not job.source_secret:
                raise ValueError("URL job is missing its protected source")
            raw_url = decrypt_source_secret(
                job.source_secret,
                job.id,
                self.settings.job_source_encryption_key,
            )
            resolver = SourceResolver([DirectHTTPVideoAdapter(), YtDlpURLAdapter()])
            return GenericURLVideoSource(raw_url, resolver, ingestion_config)
        if job.source_type == SourceType.UPLOAD:
            reference = storage_reference(job.source_reference)
            downloader = ObjectStorageDownloader.from_config(
                endpoint=self.settings.object_storage_endpoint,
                access_key=self.settings.object_storage_access_key,
                secret_key=self.settings.object_storage_secret_key,
                region=self.settings.object_storage_region,
                addressing_style=self.settings.object_storage_addressing_style,
            )
            return ObjectStorageVideoSource(reference, downloader, ingestion_config)
        raise ValueError("Unsupported job source type")


def processing_config(value: object) -> ProcessingConfig:
    if not isinstance(value, dict) or not set(value).issubset(_PROCESSING_FIELDS):
        raise ValueError("Processing config contains unsupported fields")
    if set(value) != _PROCESSING_FIELDS:
        raise ValueError("Processing config is incomplete")
    candidate_fps = value["candidate_fps"]
    window = value["selection_window_seconds"]
    if isinstance(candidate_fps, bool) or isinstance(window, bool):
        raise ValueError("Processing config values must be numbers")
    if not isinstance(candidate_fps, (int, float)) or not isinstance(
        window, (int, float)
    ):
        raise ValueError("Processing config values must be numbers")
    if candidate_fps > 60 or window > 3600:
        raise ValueError("Processing config value is out of range")
    return ProcessingConfig(
        candidate_fps=float(candidate_fps),
        selection_window_seconds=float(window),
    )


def storage_reference(value: object) -> S3ObjectReference:
    if not isinstance(value, dict) or not set(value).issubset(_REFERENCE_FIELDS):
        raise ValueError("Storage reference is invalid")
    required = {"schema_version", "bucket", "object_key"}
    if not required.issubset(value):
        raise ValueError("Storage reference is incomplete")
    if value["schema_version"] != 1:
        raise ValueError("Storage reference version is unsupported")
    if not isinstance(value["bucket"], str) or not value["bucket"]:
        raise ValueError("Storage bucket is invalid")
    object_key = value["object_key"]
    if not isinstance(object_key, str) or not object_key.startswith("jobs/"):
        raise ValueError("Storage object key is invalid")
    return S3ObjectReference(**value)


def decrypt_source_secret(value: str, job_id: UUID, encoded_key: str) -> str:
    try:
        payload = base64.b64decode(value, altchars=b"-_", validate=True)
        plaintext = AESGCM(decode_encryption_key(encoded_key)).decrypt(
            payload[:12], payload[12:], job_id.bytes
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, ValueError) as error:
        raise ValueError("Protected source could not be decrypted") from error


def result_summary(summary: ProcessingSummary) -> dict[str, int | float]:
    return {
        "frames_saved": summary.selected_frames,
        "candidates": summary.candidate_frames,
        "shortlisted": summary.shortlisted_frames,
        "duplicates_removed": summary.duplicate_frames,
        "processing_seconds": summary.processing_seconds,
        "duration_seconds": summary.duration_seconds,
    }
