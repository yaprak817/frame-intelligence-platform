from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ValidationError

from app.domain.jobs import JobStatus
from app.models.processing_job import ProcessingJob
from app.repositories.jobs import JobRepository
from app.schemas.artifacts import (
    FRAME_FILENAME_PATTERN,
    FrameAccessResponse,
    ManifestFrameV1,
    PublicFrame,
    PublicResultManifest,
    StoredManifestV1,
)
from app.storage.s3 import (
    ObjectNotFoundError,
    ObjectTooLargeError,
    ResultObjectStorage,
)

MANIFEST_MAX_BYTES = 1024 * 1024
MANIFEST_CONTENT_TYPE = "application/json"
FRAME_CONTENT_TYPE = "image/jpeg"


class ResultArtifactError(RuntimeError):
    pass


class ResultJobNotFoundError(ResultArtifactError):
    pass


class ResultNotReadyError(ResultArtifactError):
    pass


class ResultUnavailableError(ResultArtifactError):
    pass


class FailedResultUnavailableError(ResultArtifactError):
    pass


class ManifestInvalidError(ResultArtifactError):
    pass


class ArtifactNotFoundError(ResultArtifactError):
    pass


@dataclass(frozen=True)
class ParsedResultReference:
    manifest_key: str
    run_token: UUID


class ResultArtifactService:
    def __init__(
        self,
        repository: JobRepository,
        storage: ResultObjectStorage,
        url_ttl_seconds: int,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._url_ttl_seconds = url_ttl_seconds

    async def public_manifest(self, job_id: UUID) -> PublicResultManifest:
        _job, manifest = await self._load(job_id)
        return _public_manifest(manifest)

    async def frame_access(self, job_id: UUID, frame_index: int) -> FrameAccessResponse:
        _job, manifest = await self._load(job_id)
        if frame_index < 0 or frame_index >= len(manifest.frames):
            raise ArtifactNotFoundError
        frame = manifest.frames[frame_index]
        if frame.index != frame_index:
            raise ManifestInvalidError
        try:
            metadata = await self._storage.head(frame.object_key)
        except ObjectNotFoundError as error:
            raise ArtifactNotFoundError from error
        if (
            metadata.size_bytes != frame.size_bytes
            or metadata.content_type != frame.content_type
        ):
            raise ManifestInvalidError
        signed = await self._storage.presign(frame.object_key, self._url_ttl_seconds)
        return FrameAccessResponse(
            url=signed.url,
            expires_at=signed.expires_at,
            content_type=frame.content_type,
            size_bytes=frame.size_bytes,
            sha256=frame.sha256,
        )

    async def _load(self, job_id: UUID) -> tuple[ProcessingJob, StoredManifestV1]:
        job = await self._repository.get(job_id)
        if job is None:
            raise ResultJobNotFoundError
        status = JobStatus(job.status)
        if status is JobStatus.FAILED:
            raise FailedResultUnavailableError
        if status is not JobStatus.SUCCEEDED:
            raise ResultNotReadyError
        if not job.result_reference:
            raise ResultUnavailableError
        try:
            reference = parse_result_reference(
                job.result_reference, job.id, self._storage.bucket
            )
        except ValueError as error:
            raise ResultUnavailableError from error
        try:
            stored = await self._storage.read_bounded(
                reference.manifest_key, MANIFEST_MAX_BYTES
            )
        except ObjectNotFoundError as error:
            raise ResultUnavailableError from error
        except ObjectTooLargeError as error:
            raise ManifestInvalidError from error
        if stored.metadata.content_type != MANIFEST_CONTENT_TYPE:
            raise ManifestInvalidError
        manifest = validate_manifest(stored.payload, job.id, reference.run_token)
        return job, manifest


def parse_result_reference(
    value: str, job_id: UUID, configured_bucket: str
) -> ParsedResultReference:
    if "\\" in value:
        raise ValueError("Backslashes are forbidden")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError("Malformed result reference") from error
    if (
        parsed.scheme != "s3"
        or parsed.netloc != configured_bucket
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid result reference")
    if parsed.path.startswith("//") or not parsed.path.startswith("/"):
        raise ValueError("Invalid result path")
    raw_parts = parsed.path[1:].split("/")
    if any(not part or part in {".", ".."} for part in raw_parts):
        raise ValueError("Invalid result path component")
    if len(raw_parts) != 5:
        raise ValueError("Invalid result path")
    jobs, path_job, results, raw_run_token, manifest = (
        raw_parts[0],
        raw_parts[1],
        raw_parts[2],
        raw_parts[3],
        raw_parts[4:],
    )
    if jobs != "jobs" or path_job != str(job_id) or results != "results":
        raise ValueError("Result reference scope mismatch")
    if manifest != ["manifest.json"]:
        raise ValueError("Invalid manifest path")
    run_token = UUID(raw_run_token)
    if raw_run_token != str(run_token):
        raise ValueError("Run token must use canonical UUID form")
    return ParsedResultReference("/".join(raw_parts), run_token)


def validate_manifest(
    payload: bytes, job_id: UUID, run_token: UUID
) -> StoredManifestV1:
    try:
        manifest = StoredManifestV1.model_validate_json(payload)
    except ValidationError as error:
        raise ManifestInvalidError from error
    if manifest.job_id != job_id or manifest.run_token != run_token:
        raise ManifestInvalidError
    if manifest.summary.frames_saved != len(manifest.frames):
        raise ManifestInvalidError
    expected_prefix = ("jobs", str(job_id), "results", str(run_token), "frames")
    for expected_index, frame in enumerate(manifest.frames):
        _validate_frame(frame, expected_index, expected_prefix)
    return manifest


def _validate_frame(
    frame: ManifestFrameV1, expected_index: int, expected_prefix: tuple[str, ...]
) -> None:
    match = FRAME_FILENAME_PATTERN.fullmatch(frame.filename)
    key_parts = frame.object_key.split("/")
    if (
        frame.index != expected_index
        or match is None
        or int(match["index"]) != frame.index
        or int(match["timestamp"]) != frame.timestamp_ms
        or int(match["width"]) != frame.width
        or int(match["height"]) != frame.height
        or frame.content_type != FRAME_CONTENT_TYPE
        or tuple(key_parts) != (*expected_prefix, frame.filename)
        or any(not part or part in {".", ".."} for part in key_parts)
        or "\\" in frame.object_key
    ):
        raise ManifestInvalidError


def _public_manifest(manifest: StoredManifestV1) -> PublicResultManifest:
    frames = [
        PublicFrame(
            index=frame.index,
            filename=frame.filename,
            content_type=frame.content_type,
            size_bytes=frame.size_bytes,
            sha256=frame.sha256,
            timestamp_ms=frame.timestamp_ms,
            width=frame.width,
            height=frame.height,
            access_url=(
                f"/api/v1/jobs/{manifest.job_id}/result/frames/{frame.index}/access"
            ),
        )
        for frame in manifest.frames
    ]
    return PublicResultManifest(
        schema_version=manifest.schema_version,
        job_id=manifest.job_id,
        created_at=manifest.created_at,
        summary=manifest.summary,
        frames=frames,
    )
