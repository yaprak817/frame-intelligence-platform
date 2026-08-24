import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import boto3
import httpx
import psycopg
import pytest
from celery.contrib.testing.worker import start_worker

from frame_worker.artifacts.object_storage import (
    ArtifactStorageError,
    ObjectStorageArtifactStore,
)
from frame_worker.ingestion.models import NormalizedLocalVideo
from frame_worker.ingestion.validation import validate_file_basics
from frame_worker.orchestration import tasks
from frame_worker.orchestration.celery_app import celery_app, settings
from frame_worker.orchestration.repository import JobRepository
from frame_worker.orchestration.runner import JobRunner, decrypt_source_secret
from frame_worker.processing.pipeline import ProcessingSummary, SelectedFrame

pytestmark = pytest.mark.skipif(
    os.environ.get("ASYNC_E2E_INTEGRATION") != "1",
    reason="ASYNC_E2E_INTEGRATION=1 is required",
)


class TestSafeURLSource:
    __test__ = False

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def materialize(self):
        yield NormalizedLocalVideo(
            self.path, "video/mp4", "test-safe", "source.mp4", None, False
        )


class LightweightProcessor:
    def __init__(self, _config) -> None:
        pass

    def process(self, _video_path, output_directory):
        output_directory.mkdir(parents=True)
        filename = "frame_000000_1000ms_640x480.jpg"
        path = output_directory / filename
        path.write_bytes(b"test-selected-jpeg")
        frame = SelectedFrame(0, 1000, 640, 480, filename, path)
        return ProcessingSummary(
            2, 2, 1.0, 2, 1, 1, 0, 0.01, output_directory, (frame,)
        )


class CountingProcessor(LightweightProcessor):
    executions = 0

    def process(self, video_path, output_directory):
        type(self).executions += 1
        return super().process(video_path, output_directory)


class UnavailableArtifactStore:
    run_tokens = []

    def persist(self, _job_id, run_token, *_args, **_kwargs):
        type(self).run_tokens.append(run_token)
        raise ArtifactStorageError("secret object-storage endpoint")


class RecoveringArtifactStore:
    attempts = 0
    run_tokens = []

    def __init__(self) -> None:
        self.store = ObjectStorageArtifactStore.from_config(
            endpoint=settings.object_storage_endpoint,
            access_key=settings.object_storage_access_key,
            secret_key=settings.object_storage_secret_key,
            bucket=settings.object_storage_bucket,
            region=settings.object_storage_region,
            addressing_style=settings.object_storage_addressing_style,
        )

    def persist(self, job_id, run_token, summary, summary_document):
        type(self).attempts += 1
        type(self).run_tokens.append(run_token)
        if self.attempts == 1:
            raise ArtifactStorageError("temporary MinIO outage")
        return self.store.persist(job_id, run_token, summary, summary_document)

    def cleanup(self, artifacts):
        self.store.cleanup(artifacts)


class TestSafeURLRunner(JobRunner):
    __test__ = False

    def __init__(self, *args, source, expected_url, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.source = source
        self.expected_url = expected_url

    def _source(self, job):
        assert job.source_secret
        assert (
            decrypt_source_secret(
                job.source_secret, job.id, self.settings.job_source_encryption_key
            )
            == self.expected_url
        )
        return self.source


class TestSafeUploadValidator:
    def validate(self, path: Path, max_bytes: int) -> None:
        validate_file_basics(path, max_bytes)


class TestSafeUploadRunner(JobRunner):
    __test__ = False

    def _source(self, job):
        source = super()._source(job)
        source.validator = TestSafeUploadValidator()
        return source


def _artifact_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.object_storage_endpoint,
        aws_access_key_id=settings.object_storage_access_key,
        aws_secret_access_key=settings.object_storage_secret_key,
        region_name=settings.object_storage_region,
    )


def _load_manifest(result_reference: str) -> tuple[dict, str]:
    prefix = f"s3://{settings.object_storage_bucket}/"
    assert result_reference.startswith(prefix)
    assert "?" not in result_reference
    key = result_reference.removeprefix(prefix)
    response = _artifact_client().get_object(
        Bucket=settings.object_storage_bucket, Key=key
    )
    import json

    return json.loads(response["Body"].read()), key


def _cleanup_result(manifest: dict, manifest_key: str) -> None:
    client = _artifact_client()
    for frame in manifest["frames"]:
        client.delete_object(
            Bucket=settings.object_storage_bucket, Key=frame["object_key"]
        )
    client.delete_object(Bucket=settings.object_storage_bucket, Key=manifest_key)


def test_real_publisher_redis_celery_url_job_succeeds(monkeypatch, tmp_path) -> None:
    raw_url = "https://example.invalid/video?token=integration-secret"
    video = tmp_path / "source.mp4"
    video.write_bytes(b"test-safe-source-boundary")

    def build_test_runner() -> JobRunner:
        repository = JobRepository.from_url(settings.database_url)
        return TestSafeURLRunner(
            settings,
            repository,
            lambda: JobRepository.from_url(settings.database_url),
            LightweightProcessor,
            source=TestSafeURLSource(video),
            expected_url=raw_url,
        )

    monkeypatch.setattr(tasks, "build_runner", build_test_runner)
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    psycopg_url = settings.database_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    job_id = None
    with start_worker(
        celery_app,
        pool="solo",
        queues=("video-processing",),
        perform_ping_check=False,
    ):
        response = httpx.post(
            f"{backend_url}/api/v1/jobs/url",
            headers={"Idempotency-Key": f"async-url-e2e-{uuid4()}"},
            json={
                "url": raw_url,
                "processing": {
                    "candidate_fps": 2,
                    "selection_window_seconds": 1,
                },
            },
            timeout=10,
        )
        response.raise_for_status()
        assert raw_url not in response.text
        job_id = UUID(response.json()["job_id"])

        deadline = time.monotonic() + 15
        row = None
        while time.monotonic() < deadline:
            with psycopg.connect(psycopg_url) as connection:
                row = connection.execute(
                    "SELECT status, result_summary, result_reference, source_secret, "
                    "failure_message, run_token, lease_expires_at "
                    "FROM processing_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
            if row and row[0] == "SUCCEEDED":
                break
            time.sleep(0.1)
        assert row
        assert row[0] == "SUCCEEDED"
        assert row[1] == {
            "frames_saved": 1,
            "candidates": 2,
            "shortlisted": 1,
            "duplicates_removed": 0,
            "processing_seconds": 0.01,
            "duration_seconds": 1.0,
        }
        assert row[2]
        manifest, manifest_key = _load_manifest(row[2])
        assert manifest["summary"] == row[1]
        assert len(manifest["frames"]) == 1
        assert raw_url not in str(manifest)
        assert raw_url not in row[2]
        assert raw_url not in row[3]
        assert row[4] is None
        assert row[5] is None
        assert row[6] is None

    assert job_id
    _cleanup_result(manifest, manifest_key)
    with psycopg.connect(psycopg_url) as connection:
        payload = connection.execute(
            "SELECT payload FROM job_outbox WHERE aggregate_id=%s", (job_id,)
        ).fetchone()[0]
        assert payload == {"job_id": str(job_id)}
        connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))


def test_real_publisher_redis_celery_upload_job_succeeds(monkeypatch) -> None:
    def build_test_runner() -> JobRunner:
        repository = JobRepository.from_url(settings.database_url)
        return TestSafeUploadRunner(
            settings,
            repository,
            lambda: JobRepository.from_url(settings.database_url),
            LightweightProcessor,
        )

    monkeypatch.setattr(tasks, "build_runner", build_test_runner)
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    psycopg_url = settings.database_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    job_id = None
    reference = None
    with start_worker(
        celery_app,
        pool="solo",
        queues=("video-processing",),
        perform_ping_check=False,
    ):
        response = httpx.post(
            f"{backend_url}/api/v1/jobs/upload",
            headers={"Idempotency-Key": f"async-upload-e2e-{uuid4()}"},
            files={"file": ("clip.mp4", b"test-safe-upload", "video/mp4")},
            timeout=10,
        )
        response.raise_for_status()
        job_id = UUID(response.json()["job_id"])

        deadline = time.monotonic() + 15
        row = None
        while time.monotonic() < deadline:
            with psycopg.connect(psycopg_url) as connection:
                row = connection.execute(
                    "SELECT status, result_summary, result_reference, "
                    "source_reference, run_token, lease_expires_at "
                    "FROM processing_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
            if row and row[0] == "SUCCEEDED":
                break
            time.sleep(0.1)
        assert row
        assert row[0] == "SUCCEEDED"
        assert row[1] == {
            "frames_saved": 1,
            "candidates": 2,
            "shortlisted": 1,
            "duplicates_removed": 0,
            "processing_seconds": 0.01,
            "duration_seconds": 1.0,
        }
        assert row[2]
        manifest, manifest_key = _load_manifest(row[2])
        assert manifest["summary"] == row[1]
        assert len(manifest["frames"]) == 1
        reference = row[3]
        assert reference["object_key"].startswith(f"jobs/{job_id}/source/")
        assert row[4] is None
        assert row[5] is None

    assert job_id and reference
    _cleanup_result(manifest, manifest_key)
    s3 = _artifact_client()
    s3.delete_object(Bucket=reference["bucket"], Key=reference["object_key"])
    with psycopg.connect(psycopg_url) as connection:
        payload = connection.execute(
            "SELECT payload FROM job_outbox WHERE aggregate_id=%s", (job_id,)
        ).fetchone()[0]
        assert payload == {"job_id": str(job_id)}
        connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))


def test_duplicate_real_redis_deliveries_are_safe_no_ops(monkeypatch, tmp_path) -> None:
    raw_url = "https://example.invalid/video?token=duplicate-secret"
    video = tmp_path / "source.mp4"
    video.write_bytes(b"test-safe-source-boundary")
    CountingProcessor.executions = 0

    def build_test_runner() -> JobRunner:
        repository = JobRepository.from_url(settings.database_url)
        return TestSafeURLRunner(
            settings,
            repository,
            lambda: JobRepository.from_url(settings.database_url),
            CountingProcessor,
            source=TestSafeURLSource(video),
            expected_url=raw_url,
        )

    monkeypatch.setattr(tasks, "build_runner", build_test_runner)
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    psycopg_url = settings.database_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    job_id = None
    with start_worker(
        celery_app,
        pool="solo",
        queues=("video-processing",),
        perform_ping_check=False,
    ):
        response = httpx.post(
            f"{backend_url}/api/v1/jobs/url",
            headers={"Idempotency-Key": f"duplicate-e2e-{uuid4()}"},
            json={
                "url": raw_url,
                "processing": {
                    "candidate_fps": 2,
                    "selection_window_seconds": 1,
                },
            },
            timeout=10,
        )
        response.raise_for_status()
        job_id = UUID(response.json()["job_id"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with psycopg.connect(psycopg_url) as connection:
                row = connection.execute(
                    "SELECT status, attempt_count, result_reference "
                    "FROM processing_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
            if row and row[0] == "SUCCEEDED":
                break
            time.sleep(0.1)
        assert row[0:2] == ("SUCCEEDED", 1)
        original_reference = row[2]
        manifest, manifest_key = _load_manifest(original_reference)

        for _ in range(2):
            celery_app.send_task(
                "frame_worker.process_video",
                kwargs={"job_id": str(job_id)},
                queue="video-processing",
            )
        time.sleep(2)
        with psycopg.connect(psycopg_url) as connection:
            row = connection.execute(
                "SELECT status, attempt_count, result_reference "
                "FROM processing_jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        assert row == ("SUCCEEDED", 1, original_reference)
        assert CountingProcessor.executions == 1

    assert job_id
    _cleanup_result(manifest, manifest_key)
    with psycopg.connect(psycopg_url) as connection:
        connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))


def test_real_celery_retry_exhaustion_fails_without_stranding(
    monkeypatch, tmp_path
) -> None:
    raw_url = "https://example.invalid/video?token=retry-secret"
    video = tmp_path / "source.mp4"
    video.write_bytes(b"test-safe-source-boundary")
    UnavailableArtifactStore.run_tokens = []

    def build_test_runner() -> JobRunner:
        repository = JobRepository.from_url(settings.database_url)
        return TestSafeURLRunner(
            settings,
            repository,
            lambda: JobRepository.from_url(settings.database_url),
            LightweightProcessor,
            lambda: UnavailableArtifactStore(),
            source=TestSafeURLSource(video),
            expected_url=raw_url,
        )

    monkeypatch.setattr(tasks, "build_runner", build_test_runner)
    monkeypatch.setattr(tasks.random, "uniform", lambda *_args: 0)
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    psycopg_url = settings.database_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    job_id = None
    with start_worker(
        celery_app,
        pool="solo",
        queues=("video-processing",),
        perform_ping_check=False,
    ):
        response = httpx.post(
            f"{backend_url}/api/v1/jobs/url",
            headers={"Idempotency-Key": f"retry-e2e-{uuid4()}"},
            json={
                "url": raw_url,
                "processing": {
                    "candidate_fps": 2,
                    "selection_window_seconds": 1,
                },
            },
            timeout=10,
        )
        response.raise_for_status()
        job_id = UUID(response.json()["job_id"])
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            with psycopg.connect(psycopg_url) as connection:
                row = connection.execute(
                    "SELECT status, attempt_count, failure_code, failure_message, "
                    "run_token, lease_expires_at FROM processing_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
            if row and row[0] == "FAILED":
                break
            time.sleep(0.1)
        assert row == (
            "FAILED",
            3,
            "STORAGE_UNAVAILABLE",
            "Object storage is temporarily unavailable.",
            None,
            None,
        )
        assert len(UnavailableArtifactStore.run_tokens) == 3
        assert len(set(UnavailableArtifactStore.run_tokens)) == 3

    assert job_id
    with psycopg.connect(psycopg_url) as connection:
        connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))


def test_storage_outage_recovers_on_new_run(monkeypatch, tmp_path) -> None:
    raw_url = "https://example.invalid/video?token=recovery-secret"
    video = tmp_path / "source.mp4"
    video.write_bytes(b"test-safe-source-boundary")
    RecoveringArtifactStore.attempts = 0
    RecoveringArtifactStore.run_tokens = []

    def build_test_runner() -> JobRunner:
        repository = JobRepository.from_url(settings.database_url)
        return TestSafeURLRunner(
            settings,
            repository,
            lambda: JobRepository.from_url(settings.database_url),
            LightweightProcessor,
            lambda: RecoveringArtifactStore(),
            source=TestSafeURLSource(video),
            expected_url=raw_url,
        )

    monkeypatch.setattr(tasks, "build_runner", build_test_runner)
    monkeypatch.setattr(tasks.random, "uniform", lambda *_args: 0)
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    psycopg_url = settings.database_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    job_id = None
    with start_worker(
        celery_app,
        pool="solo",
        queues=("video-processing",),
        perform_ping_check=False,
    ):
        response = httpx.post(
            f"{backend_url}/api/v1/jobs/url",
            headers={"Idempotency-Key": f"storage-recovery-e2e-{uuid4()}"},
            json={
                "url": raw_url,
                "processing": {
                    "candidate_fps": 2,
                    "selection_window_seconds": 1,
                },
            },
            timeout=10,
        )
        response.raise_for_status()
        job_id = UUID(response.json()["job_id"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with psycopg.connect(psycopg_url) as connection:
                row = connection.execute(
                    "SELECT status, attempt_count, result_reference, run_token, "
                    "lease_expires_at FROM processing_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
            if row and row[0] == "SUCCEEDED":
                break
            time.sleep(0.1)
        assert row[0:2] == ("SUCCEEDED", 2)
        assert row[2]
        assert row[3] is None
        assert row[4] is None
        assert len(RecoveringArtifactStore.run_tokens) == 2
        assert len(set(RecoveringArtifactStore.run_tokens)) == 2
        manifest, manifest_key = _load_manifest(row[2])
        assert manifest["run_token"] == str(RecoveringArtifactStore.run_tokens[1])
        assert raw_url not in str(manifest)

    assert job_id
    _cleanup_result(manifest, manifest_key)
    with psycopg.connect(psycopg_url) as connection:
        connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))
