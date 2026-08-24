import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config

from frame_worker.artifacts.manifest import manifest_bytes
from frame_worker.artifacts.models import FrameManifestEntry
from frame_worker.processing.pipeline import ProcessingSummary, SelectedFrame

logger = logging.getLogger(__name__)
FRAME_CONTENT_TYPE = "image/jpeg"
MANIFEST_CONTENT_TYPE = "application/json"
_FRAME_NAME = re.compile(
    r"^frame_(?P<index>\d{6})_(?P<timestamp>\d+)ms_"
    r"(?P<width>\d+)x(?P<height>\d+)\.jpg$"
)


class ArtifactStorageError(RuntimeError):
    """Raised when durable result persistence cannot be completed."""


@dataclass(frozen=True)
class PersistedArtifacts:
    result_reference: str
    prefix: str
    object_keys: tuple[str, ...]


def artifact_prefix(job_id: UUID, run_token: UUID) -> str:
    return f"jobs/{job_id}/results/{run_token}"


class ObjectStorageArtifactStore:
    def __init__(self, client: Any, bucket: str) -> None:
        if not bucket or "/" in bucket or "\\" in bucket:
            raise ValueError("Artifact bucket is invalid")
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_config(
        cls,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
        addressing_style: str = "path",
    ) -> "ObjectStorageArtifactStore":
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(s3={"addressing_style": addressing_style}),
        )
        return cls(client, bucket)

    def persist(
        self,
        job_id: UUID,
        run_token: UUID,
        summary: ProcessingSummary,
        summary_document: dict[str, int | float],
    ) -> PersistedArtifacts:
        prefix = artifact_prefix(job_id, run_token)
        frames = self._validated_frames(summary)
        uploaded: list[str] = []
        entries: list[FrameManifestEntry] = []
        try:
            for frame in frames:
                object_key = f"{prefix}/frames/{frame.filename}"
                digest, size = _file_digest(frame.path)
                with frame.path.open("rb") as body:
                    self.client.upload_fileobj(
                        body,
                        self.bucket,
                        object_key,
                        ExtraArgs={"ContentType": FRAME_CONTENT_TYPE},
                    )
                uploaded.append(object_key)
                entries.append(
                    FrameManifestEntry(
                        index=frame.index,
                        filename=frame.filename,
                        object_key=object_key,
                        content_type=FRAME_CONTENT_TYPE,
                        size_bytes=size,
                        sha256=digest,
                        timestamp_ms=frame.timestamp_ms,
                        width=frame.width,
                        height=frame.height,
                    )
                )

            manifest_key = f"{prefix}/manifest.json"
            body = manifest_bytes(
                job_id=job_id,
                run_token=run_token,
                created_at=datetime.now(UTC),
                summary=summary_document,
                frames=tuple(entries),
            )
            self.client.put_object(
                Bucket=self.bucket,
                Key=manifest_key,
                Body=body,
                ContentType=MANIFEST_CONTENT_TYPE,
            )
            uploaded.append(manifest_key)
            return PersistedArtifacts(
                result_reference=f"s3://{self.bucket}/{manifest_key}",
                prefix=prefix,
                object_keys=tuple(uploaded),
            )
        except Exception as error:
            self._delete_keys(tuple(uploaded), prefix)
            if isinstance(error, (ValueError, ArtifactStorageError)):
                raise
            raise ArtifactStorageError("Artifact storage is unavailable") from error

    def cleanup(self, artifacts: PersistedArtifacts) -> None:
        self._delete_keys(artifacts.object_keys, artifacts.prefix)

    def _delete_keys(self, keys: tuple[str, ...], prefix: str) -> None:
        for key in reversed(keys):
            if not _belongs_to_prefix(key, prefix):
                logger.warning("Artifact cleanup rejected an invalid object key")
                continue
            try:
                self.client.delete_object(Bucket=self.bucket, Key=key)
            except Exception:
                logger.warning(
                    "Artifact cleanup failed key=%s",
                    key,
                )

    @staticmethod
    def _validated_frames(summary: ProcessingSummary) -> tuple[SelectedFrame, ...]:
        frames = tuple(sorted(summary.frames, key=lambda frame: frame.index))
        if len(frames) != summary.selected_frames:
            raise ValueError("Selected frame count does not match processing summary")
        output_directory = summary.output_directory.resolve()
        for expected_index, frame in enumerate(frames):
            match = _FRAME_NAME.fullmatch(frame.filename)
            if (
                frame.index != expected_index
                or match is None
                or int(match["index"]) != frame.index
                or int(match["timestamp"]) != frame.timestamp_ms
                or int(match["width"]) != frame.width
                or int(match["height"]) != frame.height
            ):
                raise ValueError("Selected frame metadata is invalid")
            path = frame.path
            if path.is_symlink() or not path.is_file():
                raise ValueError("Selected frame must be a regular file")
            resolved = path.resolve()
            if resolved.parent != output_directory or resolved.name != frame.filename:
                raise ValueError("Selected frame escaped the output directory")
        return frames


def _file_digest(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _belongs_to_prefix(key: str, prefix: str) -> bool:
    key_path = PurePosixPath(key)
    prefix_path = PurePosixPath(prefix)
    return key_path.parts[: len(prefix_path.parts)] == prefix_path.parts
