# ruff: noqa: E501
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
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
    PublicDatasetImage,
    PublicDatasetManifest,
    PublicFrame,
    PublicResultManifest,
    StoredDatasetManifestV1,
    StoredManifestV1,
)
from app.storage.s3 import (
    ObjectNotFoundError,
    ObjectStream,
    ObjectTooLargeError,
    ResultObjectStorage,
)

MANIFEST_MAX_BYTES = 4 * 1024 * 1024
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
        dataset_image_max_bytes: int = 50 * 1024 * 1024,
        dataset_export_max_bytes: int = 2 * 1024 * 1024 * 1024,
        spool_min_free_bytes: int = 256 * 1024 * 1024,
        spool_root: str | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._url_ttl_seconds = url_ttl_seconds
        self._dataset_image_max_bytes = dataset_image_max_bytes
        self._dataset_export_max_bytes = dataset_export_max_bytes
        self._spool_min_free_bytes = spool_min_free_bytes
        self._spool_root = spool_root

    async def public_manifest(
        self, job_id: UUID
    ) -> PublicResultManifest | PublicDatasetManifest:
        _job, manifest = await self._load(job_id)
        if isinstance(manifest, StoredDatasetManifestV1):
            return _public_dataset_manifest(manifest)
        return _public_manifest(manifest)

    async def frame_access(self, job_id: UUID, frame_index: int) -> FrameAccessResponse:
        _job, manifest = await self._load(job_id)
        frames = (
            manifest.images
            if isinstance(manifest, StoredDatasetManifestV1)
            else manifest.frames
        )
        if frame_index < 0 or frame_index >= len(frames):
            raise ArtifactNotFoundError
        frame = frames[frame_index]
        if frame.index != frame_index or frame.object_key is None:
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

    async def frame_download(
        self, job_id: UUID, frame_index: int
    ) -> tuple[ObjectStream, str]:
        _job, manifest = await self._load(job_id)
        frames = (
            manifest.images
            if isinstance(manifest, StoredDatasetManifestV1)
            else manifest.frames
        )
        if frame_index < 0 or frame_index >= len(frames):
            raise ArtifactNotFoundError
        frame = frames[frame_index]
        if frame.object_key is None:
            raise ArtifactNotFoundError
        metadata = await self._storage.head(frame.object_key)
        if (
            metadata.size_bytes != frame.size_bytes
            or metadata.content_type != frame.content_type
        ):
            raise ManifestInvalidError
        filename = (
            frame.filename
            if isinstance(manifest, StoredDatasetManifestV1)
            else _download_filename(frame.index, frame.timestamp_ms, frame.content_type)
        )
        stream = (
            await self._verified_stream(
                frame.object_key,
                frame.size_bytes,
                frame.sha256,
                frame.content_type,
                self._dataset_image_max_bytes,
            )
            if isinstance(manifest, StoredDatasetManifestV1)
            else await self._storage.open_stream(frame.object_key)
        )
        if (
            not isinstance(manifest, StoredDatasetManifestV1)
            and stream.metadata != metadata
        ):
            stream.body.close()
            raise ManifestInvalidError
        return stream, filename

    async def dataset_preview(self, job_id: UUID, image_index: int) -> ObjectStream:
        _job, manifest = await self._load(job_id)
        if not isinstance(manifest, StoredDatasetManifestV1):
            raise ArtifactNotFoundError
        if image_index < 0 or image_index >= len(manifest.images):
            raise ArtifactNotFoundError
        image = manifest.images[image_index]
        if image.index != image_index or image.object_key is None:
            raise ArtifactNotFoundError
        return await self._verified_stream(
            image.object_key,
            image.size_bytes,
            image.sha256,
            image.content_type,
            self._dataset_image_max_bytes,
        )

    async def _load(
        self, job_id: UUID
    ) -> tuple[ProcessingJob, StoredManifestV1 | StoredDatasetManifestV1]:
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
        try:
            raw = json.loads(stored.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManifestInvalidError from error
        manifest = (
            validate_dataset_manifest(stored.payload, job.id, reference.run_token)
            if isinstance(raw, dict) and raw.get("dataset_type") == "image"
            else validate_manifest(stored.payload, job.id, reference.run_token)
        )
        return job, manifest

    async def dataset_export(self, job_id: UUID, mode: str) -> tuple[ObjectStream, str]:
        _job, manifest = await self._load(job_id)
        if not isinstance(manifest, StoredDatasetManifestV1) or mode not in {
            "accepted",
            "yolo",
        }:
            raise ArtifactNotFoundError
        export = manifest.exports.get(mode)
        if export is None:
            raise ArtifactNotFoundError
        metadata = await self._storage.head(export.object_key)
        if (
            metadata.size_bytes != export.size_bytes
            or metadata.content_type != "application/zip"
            or metadata.sha256 != export.sha256
        ):
            raise ManifestInvalidError
        stream = await self._verified_stream(
            export.object_key,
            export.size_bytes,
            export.sha256,
            "application/zip",
            self._dataset_export_max_bytes,
        )
        return stream, f"image-dataset-{mode}.zip"

    async def _verified_stream(
        self,
        object_key: str,
        expected_size: int,
        expected_sha256: str,
        expected_content_type: str,
        max_bytes: int,
    ) -> ObjectStream:
        if expected_size <= 0 or expected_size > max_bytes:
            raise ManifestInvalidError
        spool_root = self._spool_root or tempfile.gettempdir()
        if shutil.disk_usage(spool_root).free < (
            expected_size + self._spool_min_free_bytes
        ):
            raise ResultUnavailableError
        source = await self._storage.open_stream(object_key)
        descriptor, path = tempfile.mkstemp(
            prefix="dataset-artifact-", suffix=".spool", dir=spool_root
        )
        digest = hashlib.sha256()
        actual = 0
        transferred = False
        try:
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in source.chunks():
                    actual += len(chunk)
                    if actual > expected_size or actual > max_bytes:
                        raise ManifestInvalidError
                    digest.update(chunk)
                    await asyncio.to_thread(output.write, chunk)
            if (
                source.metadata.size_bytes != expected_size
                or source.metadata.content_type != expected_content_type
                or actual != expected_size
                or digest.hexdigest() != expected_sha256
            ):
                raise ManifestInvalidError
            stream = ObjectStream(
                body=_DeletingFileBody(path),
                metadata=source.metadata,
            )
            transferred = True
            return stream
        finally:
            if not transferred:
                source.body.close()
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass


class _DeletingFileBody:
    def __init__(self, path: str) -> None:
        self._path = path
        self._stream = open(path, "rb")

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            try:
                os.unlink(self._path)
            except FileNotFoundError:
                pass


def _download_filename(index: int, timestamp_ms: int, content_type: str) -> str:
    extension = {"image/jpeg": "jpg", "image/png": "png"}.get(content_type)
    if extension is None:
        raise ManifestInvalidError
    hours, remainder = divmod(timestamp_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    prefix = f"frame_{index + 1:04d}_{hours:02d}-{minutes:02d}-{seconds:02d}"
    return f"{prefix}.{millis:03d}.{extension}"


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


def validate_dataset_manifest(
    payload: bytes, job_id: UUID, run_token: UUID
) -> StoredDatasetManifestV1:
    try:
        manifest = StoredDatasetManifestV1.model_validate_json(payload)
    except ValidationError as error:
        raise ManifestInvalidError from error
    if manifest.job_id != job_id or manifest.run_token != run_token:
        raise ManifestInvalidError
    prefix = f"jobs/{job_id}/results/{run_token}/"
    if set(manifest.exports) != {"accepted", "yolo"}:
        raise ManifestInvalidError
    for mode, export in manifest.exports.items():
        if (
            export.object_key != f"{prefix}exports/{mode}.zip"
            or "\\" in export.object_key
        ):
            raise ManifestInvalidError
    for expected, image in enumerate(manifest.images):
        if image.index != expected:
            raise ManifestInvalidError
        unusable = image.quality_category == "unusable"
        expected_key = f"{prefix}images/{image.filename}"
        expected_yolo = f"{prefix}yolo/{image.filename}"
        if unusable:
            if image.object_key is not None or image.yolo_object_key is not None:
                raise ManifestInvalidError
        elif image.object_key != expected_key or image.yolo_object_key != expected_yolo:
            raise ManifestInvalidError
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


def _public_dataset_manifest(
    manifest: StoredDatasetManifestV1,
) -> PublicDatasetManifest:
    images = [
        PublicDatasetImage(
            index=image.index,
            filename=image.filename,
            content_type=image.content_type,
            size_bytes=image.size_bytes,
            sha256=image.sha256,
            width=image.width,
            height=image.height,
            quality_category=image.quality_category,
            sharpness=image.sharpness,
            brightness=image.brightness,
            underexposed_ratio=image.underexposed_ratio,
            overexposed_ratio=image.overexposed_ratio,
            resolution_usable=image.resolution_usable,
            duplicate=image.duplicate,
            access_url=(
                f"/api/v1/jobs/{manifest.job_id}/result/images/{image.index}/preview"
                if image.object_key
                else None
            ),
            download_url=(
                f"/api/v1/jobs/{manifest.job_id}/result/frames/{image.index}/download"
                if image.object_key
                else None
            ),
        )
        for image in manifest.images
    ]
    return PublicDatasetManifest(
        schema_version=manifest.schema_version,
        dataset_type=manifest.dataset_type,
        job_id=manifest.job_id,
        created_at=manifest.created_at,
        summary=manifest.summary,
        recommended_indices=manifest.recommended_indices,
        images=images,
        accepted_download_url=f"/api/v1/jobs/{manifest.job_id}/dataset-exports/accepted/download",
        yolo_download_url=f"/api/v1/jobs/{manifest.job_id}/dataset-exports/yolo/download",
    )
