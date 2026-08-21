import asyncio
import hashlib
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile

from app.schemas.jobs import ProcessingConfigRequest
from app.services.job_service import JobService
from app.storage.s3 import S3MultipartUploader, UploadTooLargeError


def test_upload_api_persists_reference_and_minimal_outbox(
    client: TestClient, repository, storage
) -> None:
    response = client.post(
        "/api/v1/jobs/upload",
        headers={"Idempotency-Key": "upload-request-001"},
        files={"file": ("../clip.mp4", b"small-video", "video/mp4")},
        data={"candidate_fps": "5", "selection_window_seconds": "1"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "PENDING_DISPATCH"
    assert response.headers["location"] == response.json()["status_url"]
    job = next(iter(repository.jobs.values()))
    assert job.source_type == "UPLOAD"
    assert job.source_secret is None
    assert job.source_reference["object_key"].startswith(f"jobs/{job.id}/source/")
    assert "clip" not in job.source_reference["object_key"]
    assert repository.outbox[0].payload == {"job_id": str(job.id)}
    assert len(storage.uploads) == 1


def test_upload_idempotency_avoids_second_storage_upload(
    client: TestClient, storage
) -> None:
    kwargs = {
        "headers": {"Idempotency-Key": "upload-request-002"},
        "files": {"file": ("clip.mp4", b"same", "video/mp4")},
    }
    first = client.post("/api/v1/jobs/upload", **kwargs)
    second = client.post("/api/v1/jobs/upload", **kwargs)

    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert len(storage.uploads) == 1


@pytest.mark.parametrize(
    "file",
    [("../bad.exe", b"x", "video/mp4"), ("clip.mp4", b"x", "text/plain")],
)
def test_unsupported_upload_returns_415(client: TestClient, file) -> None:
    response = client.post(
        "/api/v1/jobs/upload",
        headers={"Idempotency-Key": "upload-invalid-01"},
        files={"file": file},
    )
    assert response.status_code == 415


class FakeS3Client:
    def __init__(self) -> None:
        self.parts: list[bytes] = []
        self.aborted = False

    def create_multipart_upload(self, **kwargs):
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs):
        self.parts.append(kwargs["Body"])
        return {"ETag": f'"{len(self.parts)}"'}

    def complete_multipart_upload(self, **kwargs):
        return {"ETag": '"complete"'}

    def abort_multipart_upload(self, **kwargs):
        self.aborted = True


def _upload_file(content: bytes) -> UploadFile:
    return UploadFile(
        BytesIO(content),
        filename="clip.mp4",
        headers=Headers({"content-type": "video/mp4"}),
    )


def test_multipart_upload_is_chunked_and_hashes_content() -> None:
    content = b"abcdefghij"
    client = FakeS3Client()
    uploader = S3MultipartUploader(
        endpoint="http://unused",
        access_key="x",
        secret_key="y",
        bucket="bucket",
        region="us-east-1",
        addressing_style="path",
        max_bytes=20,
        chunk_bytes=4,
        client=client,
    )
    reference = asyncio.run(
        uploader.upload(_upload_file(content), "jobs/id/source/original.mp4")
    )
    assert client.parts == [b"abcd", b"efgh", b"ij"]
    assert reference.size_bytes == len(content)
    assert reference.sha256 == hashlib.sha256(content).hexdigest()


def test_multipart_overflow_aborts() -> None:
    client = FakeS3Client()
    uploader = S3MultipartUploader(
        endpoint="http://unused",
        access_key="x",
        secret_key="y",
        bucket="bucket",
        region="us-east-1",
        addressing_style="path",
        max_bytes=5,
        chunk_bytes=4,
        client=client,
    )
    with pytest.raises(UploadTooLargeError):
        asyncio.run(uploader.upload(_upload_file(b"123456"), "key.mp4"))
    assert client.aborted


def test_database_failure_deletes_completed_object(repository, cipher, storage) -> None:
    repository.fail_write = True
    service = JobService(repository, cipher, storage)
    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        asyncio.run(
            service.create_upload_job(
                _upload_file(b"video"),
                "clip.mp4",
                "video/mp4",
                5,
                ProcessingConfigRequest(),
                "upload-compensation-01",
            )
        )
    assert len(storage.deletes) == 1
    assert repository.jobs == {}
    assert repository.outbox == []
