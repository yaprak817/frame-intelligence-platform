import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import boto3
from botocore.config import Config

from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.errors import (
    InvalidVideoSourceError,
    VideoDownloadError,
    VideoTooLargeError,
)
from frame_worker.ingestion.local import LocalUploadedVideoSource
from frame_worker.ingestion.models import NormalizedLocalVideo
from frame_worker.ingestion.validation import ALLOWED_VIDEO_SUFFIXES, VideoFileValidator


@dataclass(frozen=True)
class S3ObjectReference:
    schema_version: int
    bucket: str
    object_key: str
    version_id: str | None = None
    etag: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None


class ObjectStorageDownloader:
    def __init__(self, client: Any, chunk_bytes: int = 1024 * 1024) -> None:
        if chunk_bytes <= 0:
            raise ValueError("chunk_bytes must be greater than zero")
        self.client = client
        self.chunk_bytes = chunk_bytes

    @classmethod
    def from_config(
        cls,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        addressing_style: str = "path",
        chunk_bytes: int = 1024 * 1024,
    ) -> "ObjectStorageDownloader":
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(s3={"addressing_style": addressing_style}),
        )
        return cls(client, chunk_bytes)

    def download(
        self, reference: S3ObjectReference, destination: Path, max_bytes: int
    ) -> None:
        try:
            response = self.client.get_object(
                Bucket=reference.bucket,
                Key=reference.object_key,
                **({"VersionId": reference.version_id} if reference.version_id else {}),
            )
            content_length = response.get("ContentLength")
            if content_length is not None and int(content_length) > max_bytes:
                raise VideoTooLargeError(
                    "Stored video exceeds the configured size limit"
                )
            total = 0
            with destination.open("xb") as output:
                while chunk := response["Body"].read(self.chunk_bytes):
                    total += len(chunk)
                    if total > max_bytes:
                        raise VideoTooLargeError(
                            "Stored video exceeds the configured size limit"
                        )
                    output.write(chunk)
            if reference.size_bytes is not None and total != reference.size_bytes:
                raise InvalidVideoSourceError(
                    "Stored video size does not match its reference"
                )
        except (VideoTooLargeError, InvalidVideoSourceError):
            destination.unlink(missing_ok=True)
            raise
        except Exception as error:
            destination.unlink(missing_ok=True)
            raise VideoDownloadError("Object storage download failed") from error


class ObjectStorageVideoSource:
    def __init__(
        self,
        reference: S3ObjectReference,
        downloader: ObjectStorageDownloader,
        config: IngestionConfig | None = None,
        validator: VideoFileValidator | None = None,
    ) -> None:
        self.reference = reference
        self.downloader = downloader
        self.config = config or IngestionConfig()
        self.validator = validator

    @contextmanager
    def materialize(self) -> Iterator[NormalizedLocalVideo]:
        suffix = PurePosixPath(self.reference.object_key).suffix.lower()
        if suffix not in ALLOWED_VIDEO_SUFFIXES:
            raise InvalidVideoSourceError(
                "Stored object has an unsupported video suffix"
            )
        with tempfile.TemporaryDirectory(prefix="frame-worker-object-") as directory:
            workspace = Path(directory).resolve()
            partial = workspace / f"source{suffix}.part"
            final = workspace / f"source{suffix}"
            self.downloader.download(
                self.reference, partial, self.config.max_download_bytes
            )
            os.replace(partial, final)
            with LocalUploadedVideoSource(
                final,
                owns_file=False,
                config=self.config,
                validator=self.validator,
            ).materialize() as video:
                yield NormalizedLocalVideo(
                    path=video.path,
                    source_kind="object-storage",
                    original_reference=self.reference.object_key,
                    original_name=video.original_name,
                    media_type=video.media_type,
                    temporary=True,
                )
