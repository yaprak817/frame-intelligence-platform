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

from frame_worker.ingestion.errors import VideoDownloadError
from frame_worker.ingestion.models import NormalizedLocalVideo
from frame_worker.ingestion.validation import validate_file_basics
from frame_worker.orchestration import tasks
from frame_worker.orchestration.celery_app import celery_app, settings
from frame_worker.orchestration.repository import JobRepository
from frame_worker.orchestration.runner import JobRunner, decrypt_source_secret
from frame_worker.processing.pipeline import ProcessingSummary

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


class TransientURLSource:
    @contextmanager
    def materialize(self):
        raise VideoDownloadError("HTTP status 503")
        yield  # pragma: no cover


class LightweightProcessor:
    def __init__(self, _config) -> None:
        pass

    def process(self, _video_path, output_directory):
        output_directory.mkdir(parents=True)
        return ProcessingSummary(2, 2, 1.0, 2, 1, 1, 0, 0.01, output_directory)


class CountingProcessor(LightweightProcessor):
    executions = 0

    def process(self, video_path, output_directory):
        type(self).executions += 1
        return super().process(video_path, output_directory)


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
        assert row[2] is None
        assert raw_url not in row[3]
        assert row[4] is None
        assert row[5] is None
        assert row[6] is None

    assert job_id
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
        assert row[2] is None
        reference = row[3]
        assert reference["object_key"].startswith(f"jobs/{job_id}/source/")
        assert row[4] is None
        assert row[5] is None

    assert job_id and reference
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.object_storage_endpoint,
        aws_access_key_id=settings.object_storage_access_key,
        aws_secret_access_key=settings.object_storage_secret_key,
        region_name=settings.object_storage_region,
    )
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
                    "SELECT status, attempt_count FROM processing_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
            if row and row[0] == "SUCCEEDED":
                break
            time.sleep(0.1)
        assert row == ("SUCCEEDED", 1)

        for _ in range(2):
            celery_app.send_task(
                "frame_worker.process_video",
                kwargs={"job_id": str(job_id)},
                queue="video-processing",
            )
        time.sleep(2)
        with psycopg.connect(psycopg_url) as connection:
            row = connection.execute(
                "SELECT status, attempt_count FROM processing_jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        assert row == ("SUCCEEDED", 1)
        assert CountingProcessor.executions == 1

    assert job_id
    with psycopg.connect(psycopg_url) as connection:
        connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))


def test_real_celery_retry_exhaustion_fails_without_stranding(monkeypatch) -> None:
    raw_url = "https://example.invalid/video?token=retry-secret"

    def build_test_runner() -> JobRunner:
        repository = JobRepository.from_url(settings.database_url)
        return TestSafeURLRunner(
            settings,
            repository,
            lambda: JobRepository.from_url(settings.database_url),
            LightweightProcessor,
            source=TransientURLSource(),
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
            "DOWNLOAD_FAILED",
            "The video could not be downloaded.",
            None,
            None,
        )

    assert job_id
    with psycopg.connect(psycopg_url) as connection:
        connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))
