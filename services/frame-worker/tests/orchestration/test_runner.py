import base64
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from frame_worker.ingestion.errors import (
    InvalidVideoSourceError,
    UnsafeVideoURLError,
    VideoDownloadError,
    VideoTooLargeError,
)
from frame_worker.ingestion.models import NormalizedLocalVideo
from frame_worker.orchestration.config import WorkerSettings
from frame_worker.orchestration.contracts import ClaimResult, JobRecord, JobStatus
from frame_worker.orchestration.failures import classify_failure
from frame_worker.orchestration.heartbeat import LeaseHeartbeat
from frame_worker.orchestration.runner import (
    JobRunner,
    RetryableExecutionError,
    decrypt_source_secret,
    processing_config,
    result_summary,
    storage_reference,
)
from frame_worker.processing.pipeline import ProcessingSummary


def settings(tmp_path) -> WorkerSettings:
    key = base64.urlsafe_b64encode(b"T" * 32).decode()
    return WorkerSettings(
        database_url="sqlite://",
        celery_broker_url="redis://localhost/0",
        job_source_encryption_key=key,
        object_storage_endpoint="http://localhost:9000",
        object_storage_access_key="test",
        object_storage_secret_key="test",
        object_storage_region="us-east-1",
        object_storage_addressing_style="path",
        max_download_bytes=1024,
        lease_seconds=60,
        heartbeat_interval_seconds=0.01,
        visibility_timeout_seconds=3600,
        worker_concurrency=1,
        processing_temp_root=tmp_path,
    )


def job() -> JobRecord:
    return JobRecord(
        id=uuid4(),
        status=JobStatus.RUNNING,
        source_type="URL",
        source_secret="protected",
        source_reference=None,
        processing_config={
            "candidate_fps": 5.0,
            "selection_window_seconds": 1.0,
        },
        attempt_count=1,
        run_token=uuid4(),
        lease_expires_at=None,
        result_reference=None,
        result_summary=None,
        version=2,
    )


class FakeRepository:
    def __init__(self, record=None) -> None:
        self.record = record
        self.summary = None
        self.failed = None
        self.released = False
        self.heartbeats = 0

    def claim(self, _job_id, _lease):
        return ClaimResult(self.record)

    def heartbeat(self, *_args):
        self.heartbeats += 1
        return True

    def succeed(self, _job_id, _token, summary):
        self.summary = summary
        return True

    def fail(self, _job_id, _token, code, message):
        self.failed = (code, message)
        return True

    def release_for_retry(self, *_args):
        self.released = True
        return True

    def close(self):
        pass


class FakeSource:
    def __init__(self, path: Path, error=None) -> None:
        self.path = path
        self.error = error

    @contextmanager
    def materialize(self):
        if self.error:
            raise self.error
        yield NormalizedLocalVideo(self.path, "test", "safe", "test.mp4", None, True)


class FakeProcessor:
    def __init__(self, _config) -> None:
        pass

    def process(self, _video_path, output_directory):
        output_directory.mkdir(parents=True)
        (output_directory / "frame.jpg").write_bytes(b"frame")
        return ProcessingSummary(30, 60, 2.0, 10, 4, 3, 1, 0.5, output_directory)


class TestRunner(JobRunner):
    __test__ = False

    def __init__(self, *args, source, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source

    def _source(self, _job):
        return self.source


def test_success_persists_summary_and_cleans_workspace(tmp_path) -> None:
    record = job()
    repository = FakeRepository(record)
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    runner = TestRunner(
        settings(tmp_path),
        repository,
        lambda: repository,
        FakeProcessor,
        source=FakeSource(video),
    )
    assert runner.execute(record.id)
    assert repository.summary == {
        "frames_saved": 3,
        "candidates": 10,
        "shortlisted": 4,
        "duplicates_removed": 1,
        "processing_seconds": 0.5,
        "duration_seconds": 2.0,
    }
    assert not list(tmp_path.glob("frame-job-*"))


def test_retryable_storage_failure_releases_claim(tmp_path) -> None:
    record = job()
    record = JobRecord(**{**record.__dict__, "source_type": "UPLOAD"})
    repository = FakeRepository(record)
    runner = TestRunner(
        settings(tmp_path),
        repository,
        lambda: repository,
        FakeProcessor,
        source=FakeSource(tmp_path / "none", VideoDownloadError("secret endpoint")),
    )
    with pytest.raises(RetryableExecutionError) as captured:
        runner.execute(record.id)
    assert repository.released
    assert captured.value.failure.code == "STORAGE_UNAVAILABLE"
    assert "secret endpoint" not in captured.value.failure.message


def test_heartbeat_stops_and_updates_lease() -> None:
    repository = FakeRepository()
    with LeaseHeartbeat(lambda: repository, uuid4(), uuid4(), 1, 0.01):
        import time

        time.sleep(0.03)
    assert repository.heartbeats >= 1


def test_processing_config_rejects_unknown_or_paths() -> None:
    valid = {"candidate_fps": 5, "selection_window_seconds": 1}
    assert processing_config(valid).candidate_fps == 5
    with pytest.raises(ValueError):
        processing_config({**valid, "ffmpeg_binary": "arbitrary-ffmpeg"})


def test_storage_reference_is_strict() -> None:
    value = {"schema_version": 1, "bucket": "bucket", "object_key": "jobs/id/a.mp4"}
    assert storage_reference(value).bucket == "bucket"
    with pytest.raises(ValueError):
        storage_reference({**value, "url": "https://secret.invalid"})


def test_decryption_matches_backend_contract() -> None:
    key = b"K" * 32
    encoded = base64.urlsafe_b64encode(key).decode()
    job_id = uuid4()
    nonce = os.urandom(12)
    raw_url = "https://example.com/video?token=secret"
    protected = base64.urlsafe_b64encode(
        nonce + AESGCM(key).encrypt(nonce, raw_url.encode(), job_id.bytes)
    ).decode()
    assert decrypt_source_secret(protected, job_id, encoded) == raw_url


def test_result_summary_never_contains_output_path(tmp_path) -> None:
    summary = ProcessingSummary(30, 60, 2, 10, 4, 3, 1, 0.5, tmp_path)
    persisted = result_summary(summary)
    assert str(tmp_path) not in str(persisted)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (UnsafeVideoURLError("raw secret"), "UNSAFE_URL"),
        (InvalidVideoSourceError("raw secret"), "INVALID_VIDEO"),
        (VideoTooLargeError("raw secret"), "SOURCE_TOO_LARGE"),
        (VideoDownloadError("HTTP status 503 raw secret"), "DOWNLOAD_FAILED"),
    ],
)
def test_failure_mapping_is_stable_and_sanitized(error, code) -> None:
    failure = classify_failure(error, "URL")
    assert failure.code == code
    assert "raw secret" not in failure.message


def test_http_5xx_is_retryable() -> None:
    failure = classify_failure(VideoDownloadError("HTTP status 503"), "URL")
    assert failure.retryable
