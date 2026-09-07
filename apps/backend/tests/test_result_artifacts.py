import asyncio
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.api.dependencies import get_result_artifact_service
from app.domain.jobs import JobStatus
from app.models.processing_job import ProcessingJob
from app.services.result_artifacts import (
    MANIFEST_MAX_BYTES,
    ManifestInvalidError,
    ResultArtifactService,
    ResultUnavailableError,
    parse_result_reference,
    validate_manifest,
)
from app.storage.s3 import (
    BoundedObject,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStream,
    ObjectTooLargeError,
    PresignedObject,
    S3ResultObjectStorage,
)


class DirectStreamStorage:
    bucket = "frame-intelligence"

    def __init__(self, body) -> None:
        self.body = body

    async def open_stream(self, _object_key: str) -> ObjectStream:
        return ObjectStream(self.body, ObjectMetadata(4, "image/jpeg"))


def test_verified_spool_rejects_low_disk_before_open(monkeypatch, tmp_path) -> None:
    storage = DirectStreamStorage(io.BytesIO(b"data"))
    opened = False

    async def open_stream(_key):
        nonlocal opened
        opened = True
        return await DirectStreamStorage.open_stream(storage, _key)

    storage.open_stream = open_stream  # type: ignore[method-assign]
    monkeypatch.setattr(
        "app.services.result_artifacts.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": 7})(),
    )
    service = ResultArtifactService(None, storage, 300, 10, 10, 4, str(tmp_path))  # type: ignore[arg-type]
    with pytest.raises(ResultUnavailableError):
        asyncio.run(
            service._verified_stream(
                "key", 4, hashlib.sha256(b"data").hexdigest(), "image/jpeg", 10
            )
        )
    assert not opened
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("payload", [b"fail", b"dat", b"datax"])
def test_verified_spool_rejects_hash_and_size_mismatch_and_cleans(
    tmp_path, payload
) -> None:
    body = io.BytesIO(payload)
    service = ResultArtifactService(
        None, DirectStreamStorage(body), 300, 10, 10, 0, str(tmp_path)
    )  # type: ignore[arg-type]
    with pytest.raises(ManifestInvalidError):
        asyncio.run(
            service._verified_stream(
                "key", 4, hashlib.sha256(b"data").hexdigest(), "image/jpeg", 10
            )
        )
    assert body.closed
    assert list(tmp_path.iterdir()) == []


def test_verified_spool_cancellation_closes_body_and_removes_file(tmp_path) -> None:
    class CancelBody(io.BytesIO):
        def read(self, _size=-1):
            raise asyncio.CancelledError

    body = CancelBody(b"data")
    service = ResultArtifactService(
        None, DirectStreamStorage(body), 300, 10, 10, 0, str(tmp_path)
    )  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service._verified_stream(
                "key", 4, hashlib.sha256(b"data").hexdigest(), "image/jpeg", 10
            )
        )
    assert body.closed
    assert list(tmp_path.iterdir()) == []


class FakeResultStorage:
    bucket = "frame-intelligence"

    def __init__(self) -> None:
        self.payload = b""
        self.manifest_content_type = "application/json"
        self.metadata: dict[str, ObjectMetadata] = {}
        self.read_error: Exception | None = None
        self.head_error: Exception | None = None
        self.head_calls: list[str] = []
        self.presign_calls: list[tuple[str, int]] = []

    async def read_bounded(self, object_key: str, max_bytes: int) -> BoundedObject:
        assert max_bytes == MANIFEST_MAX_BYTES
        if self.read_error:
            raise self.read_error
        return BoundedObject(
            self.payload,
            ObjectMetadata(len(self.payload), self.manifest_content_type),
        )

    async def head(self, object_key: str) -> ObjectMetadata:
        self.head_calls.append(object_key)
        if self.head_error:
            raise self.head_error
        return self.metadata[object_key]

    async def presign(self, object_key: str, ttl_seconds: int) -> PresignedObject:
        self.presign_calls.append((object_key, ttl_seconds))
        return PresignedObject(
            f"http://public.example/{object_key}?signature=secret",
            datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )


def _job(job_id, status=JobStatus.SUCCEEDED, reference=True) -> ProcessingJob:
    now = datetime.now(UTC)
    run_token = uuid4()
    result_reference = (
        f"s3://frame-intelligence/jobs/{job_id}/results/{run_token}/manifest.json"
        if reference
        else None
    )
    return ProcessingJob(
        id=job_id,
        status=status,
        source_type="UPLOAD",
        source_display="clip.mp4",
        source_secret=None,
        source_reference=None,
        processing_config={},
        created_at=now,
        started_at=now,
        completed_at=now if status in {JobStatus.SUCCEEDED, JobStatus.FAILED} else None,
        failure_code=None,
        failure_message=None,
        attempt_count=1,
        idempotency_scope="POST:/api/v1/jobs/upload",
        idempotency_key=f"result-{job_id}",
        request_fingerprint="0" * 64,
        result_reference=result_reference,
        result_summary={"frames_saved": 1} if reference else None,
        run_token=None,
        lease_expires_at=None,
        version=3,
    )


def _manifest(job: ProcessingJob, *, count: int = 1) -> bytes:
    run_token = job.result_reference.split("/")[-2]
    frames = []
    for index in range(count):
        timestamp = (index + 1) * 1000
        filename = f"frame_{index:06d}_{timestamp}ms_640x480.jpg"
        frames.append(
            {
                "index": index,
                "filename": filename,
                "object_key": (f"jobs/{job.id}/results/{run_token}/frames/{filename}"),
                "content_type": "image/jpeg",
                "size_bytes": 123 + index,
                "sha256": hashlib.sha256(filename.encode()).hexdigest(),
                "timestamp_ms": timestamp,
                "width": 640,
                "height": 480,
            }
        )
    return json.dumps(
        {
            "schema_version": 1,
            "job_id": str(job.id),
            "run_token": run_token,
            "created_at": "2026-08-24T12:00:00Z",
            "summary": {
                "frames_saved": count,
                "candidates": count,
                "shortlisted": count,
                "duplicates_removed": 0,
                "processing_seconds": 1.5,
                "duration_seconds": 2.0,
            },
            "frames": frames,
        }
    ).encode()


@pytest.fixture
def result_client(client, repository):
    storage = FakeResultStorage()
    service = ResultArtifactService(repository, storage, 300)
    app = client.app
    app.dependency_overrides[get_result_artifact_service] = lambda: service
    yield client, repository, storage
    app.dependency_overrides.pop(get_result_artifact_service, None)


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.PENDING_DISPATCH,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.FAILED,
    ],
)
def test_result_rejects_every_non_succeeded_state(result_client, status) -> None:
    client, repository, _storage = result_client
    job = _job(uuid4(), status=status, reference=False)
    repository.jobs[job.id] = job

    response = client.get(f"/api/v1/jobs/{job.id}/result")

    assert response.status_code == 409
    expected = (
        "RESULT_UNAVAILABLE" if status is JobStatus.FAILED else "RESULT_NOT_READY"
    )
    assert response.json()["detail"]["code"] == expected


@pytest.mark.parametrize("job_id", [str(uuid4()), "not-a-uuid"])
def test_result_unknown_or_invalid_job_is_safe_404(result_client, job_id) -> None:
    response = result_client[0].get(f"/api/v1/jobs/{job_id}/result")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "JOB_NOT_FOUND"


@pytest.mark.parametrize(
    ("reference", "expected_code"),
    [
        (None, "RESULT_UNAVAILABLE"),
        ("not-an-s3-reference", "RESULT_UNAVAILABLE"),
        (
            "s3://wrong-bucket/jobs/{job_id}/results/{run_token}/manifest.json",
            "RESULT_UNAVAILABLE",
        ),
        (
            "s3://frame-intelligence/jobs/{other_job}/results/"
            "{run_token}/manifest.json",
            "RESULT_UNAVAILABLE",
        ),
    ],
)
def test_succeeded_job_reference_failures_are_safe(
    result_client, reference, expected_code
) -> None:
    client, repository, _storage = result_client
    job = _job(uuid4())
    run_token = uuid4()
    if reference is not None:
        reference = reference.format(
            job_id=job.id, other_job=uuid4(), run_token=run_token
        )
    job.result_reference = reference
    repository.jobs[job.id] = job

    response = client.get(f"/api/v1/jobs/{job.id}/result")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == expected_code
    for secret in (
        "frame-intelligence",
        "wrong-bucket",
        "manifest.json",
        "s3://",
        "localhost:9000",
    ):
        assert secret not in response.text


def test_reference_run_mismatching_manifest_is_safe_502(result_client) -> None:
    client, repository, storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job
    document = json.loads(_manifest(job))
    document["run_token"] = str(uuid4())
    storage.payload = json.dumps(document).encode()

    response = client.get(f"/api/v1/jobs/{job.id}/result")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MANIFEST_INVALID"
    assert "frame-intelligence" not in response.text
    assert "manifest.json" not in response.text


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "https://frame-intelligence/jobs/id/results/run/manifest.json",
        "s3://other/jobs/id/results/run/manifest.json",
        "s3://user:pass@frame-intelligence/jobs/id/results/run/manifest.json",
        "s3://frame-intelligence/jobs/id/results/run/manifest.json?x=1",
        "s3://frame-intelligence/jobs/id/results/run/manifest.json#x",
        "s3://frame-intelligence/jobs\\id/results/run/manifest.json",
        "s3://frame-intelligence/jobs//results/run/manifest.json",
        "s3://frame-intelligence/jobs/../results/run/manifest.json",
    ],
)
def test_result_reference_attack_vectors_are_rejected(reference) -> None:
    with pytest.raises(ValueError):
        parse_result_reference(reference, uuid4(), "frame-intelligence")


def test_cross_job_and_noncanonical_run_reference_are_rejected() -> None:
    job_id = uuid4()
    run_token = uuid4()
    with pytest.raises(ValueError):
        parse_result_reference(
            f"s3://frame-intelligence/jobs/{uuid4()}/results/{run_token}/manifest.json",
            job_id,
            "frame-intelligence",
        )
    with pytest.raises(ValueError):
        parse_result_reference(
            f"s3://frame-intelligence/jobs/{job_id}/results/"
            f"{str(run_token).upper()}/manifest.json",
            job_id,
            "frame-intelligence",
        )


@pytest.mark.parametrize(
    ("mutate", "description"),
    [
        (lambda data: data.update(schema_version=2), "unsupported"),
        (lambda data: data.update(job_id=str(uuid4())), "cross job"),
        (lambda data: data.update(run_token=str(uuid4())), "cross run"),
        (lambda data: data["frames"].append(data["frames"][0]), "duplicate"),
        (lambda data: data["frames"][0].update(index=1), "non-contiguous"),
        (lambda data: data["summary"].update(frames_saved=2), "count"),
        (lambda data: data["frames"][0].update(filename="../frame.jpg"), "filename"),
        (lambda data: data["frames"][0].update(sha256="A" * 64), "sha"),
        (lambda data: data["frames"][0].update(object_key="jobs/forged"), "key"),
        (lambda data: data.update(extra="forbidden"), "extra"),
    ],
)
def test_invalid_manifest_contract_is_rejected(mutate, description) -> None:
    job = _job(uuid4())
    document = json.loads(_manifest(job))
    mutate(document)
    run_token = uuid4()
    if description not in {"cross run"}:
        run_token = uuid4()
        document["run_token"] = str(run_token)
        prefix = f"jobs/{job.id}/results/{run_token}/frames/"
        for frame in document["frames"]:
            if frame["object_key"].count("/") > 1 and description != "key":
                frame["object_key"] = prefix + frame["filename"]
    with pytest.raises(ManifestInvalidError):
        validate_manifest(json.dumps(document).encode(), job.id, run_token)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_summary_values_are_rejected(value: float) -> None:
    job = _job(uuid4())
    document = json.loads(_manifest(job))
    document["summary"]["processing_seconds"] = value
    run_token = uuid4()
    document["run_token"] = str(run_token)
    document["frames"][0]["object_key"] = (
        f"jobs/{job.id}/results/{run_token}/frames/{document['frames'][0]['filename']}"
    )

    with pytest.raises(ManifestInvalidError):
        validate_manifest(json.dumps(document).encode(), job.id, run_token)


def test_non_utc_created_at_is_rejected() -> None:
    job = _job(uuid4())
    document = json.loads(_manifest(job))
    document["created_at"] = "2026-08-24T15:00:00+03:00"
    run_token = uuid4()
    document["run_token"] = str(run_token)
    document["frames"][0]["object_key"] = (
        f"jobs/{job.id}/results/{run_token}/frames/{document['frames'][0]['filename']}"
    )

    with pytest.raises(ManifestInvalidError):
        validate_manifest(json.dumps(document).encode(), job.id, run_token)


@pytest.mark.parametrize("scope", ["job", "run"])
def test_cross_scope_frame_key_is_rejected_by_endpoint(result_client, scope) -> None:
    client, repository, storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job
    document = json.loads(_manifest(job))
    frame = document["frames"][0]
    key_job = uuid4() if scope == "job" else job.id
    key_run = uuid4() if scope == "run" else document["run_token"]
    frame["object_key"] = f"jobs/{key_job}/results/{key_run}/frames/{frame['filename']}"
    storage.payload = json.dumps(document).encode()

    response = client.get(f"/api/v1/jobs/{job.id}/result")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MANIFEST_INVALID"
    for secret in (frame["object_key"], "frame-intelligence", "s3://"):
        assert secret not in response.text


def test_successful_metadata_and_download_are_sanitized(result_client) -> None:
    client, repository, storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job
    storage.payload = _manifest(job)

    metadata = client.get(f"/api/v1/jobs/{job.id}/result")
    download = client.get(f"/api/v1/jobs/{job.id}/result/manifest")

    assert metadata.status_code == 200
    frame = metadata.json()["frames"][0]
    assert set(frame) == {
        "index",
        "filename",
        "content_type",
        "size_bytes",
        "sha256",
        "timestamp_ms",
        "width",
        "height",
        "access_url",
    }
    assert "object_key" not in metadata.text
    assert "run_token" not in metadata.text
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/json")
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["content-disposition"] == (
        f'attachment; filename="job-{job.id}-manifest.json"'
    )
    assert "object_key" not in download.text


def test_frame_access_requires_matching_head_and_uses_ttl(result_client) -> None:
    client, repository, storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job
    storage.payload = _manifest(job)
    frame = json.loads(storage.payload)["frames"][0]
    storage.metadata[frame["object_key"]] = ObjectMetadata(123, "image/jpeg")

    response = client.post(f"/api/v1/jobs/{job.id}/result/frames/0/access")

    assert response.status_code == 200
    assert storage.head_calls == [frame["object_key"]]
    assert storage.presign_calls == [(frame["object_key"], 300)]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["sha256"] == frame["sha256"]


@pytest.mark.parametrize(
    ("metadata", "error", "expected_status", "expected_code"),
    [
        (ObjectMetadata(124, "image/jpeg"), None, 502, "MANIFEST_INVALID"),
        (ObjectMetadata(123, "image/png"), None, 502, "MANIFEST_INVALID"),
        (None, ObjectNotFoundError(), 404, "ARTIFACT_NOT_FOUND"),
        (None, ObjectStorageError(), 503, "STORAGE_UNAVAILABLE"),
    ],
)
def test_frame_head_failures_are_safe(
    result_client, metadata, error, expected_status, expected_code, caplog
) -> None:
    client, repository, storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job
    storage.payload = _manifest(job)
    frame = json.loads(storage.payload)["frames"][0]
    if metadata:
        storage.metadata[frame["object_key"]] = metadata
    storage.head_error = error

    response = client.post(f"/api/v1/jobs/{job.id}/result/frames/0/access")

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    assert storage.presign_calls == []
    assert frame["object_key"] not in response.text
    assert "signature=secret" not in caplog.text


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (ObjectNotFoundError(), "RESULT_UNAVAILABLE"),
        (ObjectTooLargeError(), "MANIFEST_INVALID"),
        (ObjectStorageError(), "STORAGE_UNAVAILABLE"),
    ],
)
def test_manifest_storage_failures_are_mapped(result_client, error, expected_code):
    client, repository, storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job
    storage.read_error = error

    response = client.get(f"/api/v1/jobs/{job.id}/result")

    assert response.json()["detail"]["code"] == expected_code


def test_manifest_short_read_is_safe_storage_unavailable(result_client) -> None:
    client, repository, _storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job

    class ShortBody:
        def __init__(self) -> None:
            self.close_calls = 0

        def read(self, size: int) -> bytes:
            assert size == MANIFEST_MAX_BYTES + 1
            return b"123"

        def close(self) -> None:
            self.close_calls += 1

    class ShortReadClient:
        def __init__(self, body: ShortBody) -> None:
            self.body = body

        def head_object(self, **_kwargs):
            return {"ContentLength": 4, "ContentType": "application/json"}

        def get_object(self, **_kwargs):
            return {"ContentLength": 4, "Body": self.body}

    body = ShortBody()
    storage = S3ResultObjectStorage(
        internal_endpoint="http://private-storage:9000",
        external_endpoint="https://downloads.example",
        access_key="internal-access",
        secret_key="internal-secret",
        bucket="frame-intelligence",
        region="us-east-1",
        addressing_style="path",
        internal_client=ShortReadClient(body),
        signing_client=ShortReadClient(body),
    )
    service = ResultArtifactService(repository, storage, 300)
    client.app.dependency_overrides[get_result_artifact_service] = lambda: service

    response = client.get(f"/api/v1/jobs/{job.id}/result")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "STORAGE_UNAVAILABLE"
    assert body.close_calls == 1
    for secret in (
        "frame-intelligence",
        "private-storage:9000",
        "internal-access",
        "internal-secret",
        job.result_reference,
    ):
        assert secret not in response.text


def test_manifest_metadata_mismatch_is_safe_storage_unavailable(result_client) -> None:
    client, repository, _storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job

    class UnreadBody:
        def __init__(self) -> None:
            self.close_calls = 0

        def read(self, _size: int) -> bytes:
            raise AssertionError("metadata mismatch must fail before body read")

        def close(self) -> None:
            self.close_calls += 1

    class MismatchClient:
        def __init__(self, body: UnreadBody) -> None:
            self.body = body

        def head_object(self, **_kwargs):
            return {"ContentLength": 3, "ContentType": "application/json"}

        def get_object(self, **_kwargs):
            return {"ContentLength": 4, "Body": self.body}

    body = UnreadBody()
    storage = S3ResultObjectStorage(
        internal_endpoint="http://private-storage:9000",
        external_endpoint="https://downloads.example",
        access_key="internal-access",
        secret_key="internal-secret",
        bucket="frame-intelligence",
        region="us-east-1",
        addressing_style="path",
        internal_client=MismatchClient(body),
        signing_client=MismatchClient(body),
    )
    service = ResultArtifactService(repository, storage, 300)
    client.app.dependency_overrides[get_result_artifact_service] = lambda: service

    response = client.get(f"/api/v1/jobs/{job.id}/result")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "STORAGE_UNAVAILABLE"
    assert body.close_calls == 1
    for secret in (
        "frame-intelligence",
        "private-storage:9000",
        "internal-access",
        "internal-secret",
        job.result_reference,
    ):
        assert secret not in response.text


def test_manifest_content_type_must_match_worker_contract(result_client) -> None:
    client, repository, storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job
    storage.payload = _manifest(job)
    storage.manifest_content_type = "text/plain"

    response = client.get(f"/api/v1/jobs/{job.id}/result")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MANIFEST_INVALID"


def test_unknown_frame_and_invalid_index_are_rejected(result_client) -> None:
    client, repository, storage = result_client
    job = _job(uuid4())
    repository.jobs[job.id] = job
    storage.payload = _manifest(job)
    assert (
        client.post(f"/api/v1/jobs/{job.id}/result/frames/99/access").status_code == 404
    )
    assert (
        client.post(f"/api/v1/jobs/{job.id}/result/frames/not-int/access").status_code
        == 422
    )
    assert (
        client.post(f"/api/v1/jobs/{job.id}/result/frames/-1/access").status_code == 422
    )
