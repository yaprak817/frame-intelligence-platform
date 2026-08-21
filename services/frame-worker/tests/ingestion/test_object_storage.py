import os
from io import BytesIO
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config

from frame_worker.ingestion.errors import VideoTooLargeError
from frame_worker.ingestion.object_storage import (
    ObjectStorageDownloader,
    ObjectStorageVideoSource,
    S3ObjectReference,
)


class Body:
    def __init__(self, content: bytes) -> None:
        self.stream = BytesIO(content)
        self.read_sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.stream.read(size)


class Client:
    def __init__(self, content: bytes, declared_size: int | None = None) -> None:
        self.body = Body(content)
        self.declared_size = (
            declared_size if declared_size is not None else len(content)
        )

    def get_object(self, **kwargs):
        return {"Body": self.body, "ContentLength": self.declared_size}


class Validator:
    def validate(self, path, max_bytes):
        assert path.read_bytes() == b"video-bytes"


def reference(size: int | None = 11) -> S3ObjectReference:
    return S3ObjectReference(
        1, "bucket", "jobs/id/source/original.mp4", size_bytes=size
    )


def test_object_source_downloads_in_chunks_and_cleans_workspace() -> None:
    client = Client(b"video-bytes")
    source = ObjectStorageVideoSource(
        reference(),
        ObjectStorageDownloader(client, chunk_bytes=3),
        validator=Validator(),
    )
    with source.materialize() as video:
        path = video.path
        assert path.read_bytes() == b"video-bytes"
        assert video.source_kind == "object-storage"
    assert not path.exists()
    assert all(size == 3 for size in client.body.read_sizes)


def test_known_oversize_is_rejected_without_writing(tmp_path) -> None:
    downloader = ObjectStorageDownloader(Client(b"x", declared_size=100), chunk_bytes=3)
    destination = tmp_path / "source.part"
    with pytest.raises(VideoTooLargeError):
        downloader.download(reference(None), destination, max_bytes=10)
    assert not destination.exists()


def test_runtime_overflow_removes_partial_file(tmp_path) -> None:
    downloader = ObjectStorageDownloader(
        Client(b"123456", declared_size=2), chunk_bytes=3
    )
    destination = tmp_path / "source.part"
    with pytest.raises(VideoTooLargeError):
        downloader.download(reference(None), destination, max_bytes=5)
    assert not destination.exists()


@pytest.mark.skipif(
    os.environ.get("OBJECT_STORAGE_INTEGRATION") != "1",
    reason="OBJECT_STORAGE_INTEGRATION=1 is required",
)
def test_real_minio_object_materialization_and_cleanup() -> None:
    bucket = os.environ["OBJECT_STORAGE_BUCKET"]
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["OBJECT_STORAGE_ENDPOINT"],
        aws_access_key_id=os.environ["OBJECT_STORAGE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["OBJECT_STORAGE_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    try:
        client.create_bucket(Bucket=bucket)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    key = f"jobs/{uuid4()}/source/original.mp4"
    content = b"video-bytes"
    client.put_object(Bucket=bucket, Key=key, Body=content)
    try:
        source = ObjectStorageVideoSource(
            S3ObjectReference(1, bucket, key, size_bytes=len(content)),
            ObjectStorageDownloader(client, chunk_bytes=3),
            validator=Validator(),
        )
        with source.materialize() as video:
            materialized = video.path
            assert materialized.read_bytes() == content
        assert not materialized.exists()
    finally:
        client.delete_object(Bucket=bucket, Key=key)
