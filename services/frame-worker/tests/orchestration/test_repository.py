import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, text, update

from frame_worker.orchestration.contracts import JobStatus
from frame_worker.orchestration.repository import (
    JobRepository,
    metadata,
    processing_jobs,
)


def _repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    metadata.create_all(engine)
    return JobRepository(engine), engine


def _seed(engine, status=JobStatus.QUEUED, lease_expires_at=None):
    job_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(processing_jobs).values(
                id=job_id,
                status=status,
                source_type="URL",
                source_secret="protected",
                source_reference=None,
                processing_config={
                    "candidate_fps": 5.0,
                    "selection_window_seconds": 1.0,
                },
                started_at=None,
                completed_at=None,
                failure_code=None,
                failure_message=None,
                attempt_count=0,
                result_reference=None,
                result_summary=None,
                run_token=uuid4() if status == JobStatus.RUNNING else None,
                lease_expires_at=lease_expires_at,
                version=1,
            )
        )
    return job_id


def test_only_one_delivery_claims_queued_job(tmp_path) -> None:
    repository, engine = _repository(tmp_path)
    job_id = _seed(engine)
    try:
        first = repository.claim(job_id, 60)
        second = repository.claim(job_id, 60)
        assert first.job is not None
        assert first.job.attempt_count == 1
        assert first.job.run_token is not None
        assert second.job is None
        assert not second.retry_later
    finally:
        repository.close()


def test_stale_lease_reclaims_and_old_token_cannot_complete(tmp_path) -> None:
    repository, engine = _repository(tmp_path)
    old_token = uuid4()
    job_id = _seed(
        engine,
        JobStatus.RUNNING,
        datetime.now(UTC) - timedelta(seconds=1),
    )
    with engine.begin() as connection:
        connection.execute(
            update(processing_jobs)
            .where(processing_jobs.c.id == job_id)
            .values(run_token=old_token)
        )
    try:
        claim = repository.claim(job_id, 60)
        assert claim.job is not None
        assert claim.job.run_token != old_token
        assert claim.job.attempt_count == 1
        assert not repository.succeed(job_id, old_token, {"frames_saved": 1})
        assert repository.succeed(job_id, claim.job.run_token, {"frames_saved": 2})
        stored = repository.get(job_id)
        assert stored and stored.status == JobStatus.SUCCEEDED
        assert stored.result_summary == {"frames_saved": 2}
    finally:
        repository.close()


def test_valid_lease_is_not_reclaimed(tmp_path) -> None:
    repository, engine = _repository(tmp_path)
    job_id = _seed(
        engine,
        JobStatus.RUNNING,
        datetime.now(UTC) + timedelta(minutes=5),
    )
    try:
        assert repository.claim(job_id, 60).job is None
    finally:
        repository.close()


def test_pending_dispatch_requests_retry_later(tmp_path) -> None:
    repository, engine = _repository(tmp_path)
    job_id = _seed(engine, JobStatus.PENDING_DISPATCH)
    try:
        claim = repository.claim(job_id, 60)
        assert claim.job is None
        assert claim.retry_later
    finally:
        repository.close()


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_claim_uses_migrated_postgresql_schema() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    repository = JobRepository.from_url(database_url)
    job_id = uuid4()
    now = datetime.now(UTC)
    try:
        with repository._engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO processing_jobs (
                        id, status, source_type, source_display, source_secret,
                        source_reference, processing_config, created_at,
                        started_at, completed_at, failure_code, failure_message,
                        attempt_count, idempotency_scope, idempotency_key,
                        request_fingerprint, result_reference, result_summary,
                        run_token, lease_expires_at, version
                    ) VALUES (
                        :id, 'QUEUED', 'URL', 'https://example.com/video',
                        'protected', NULL, CAST(:config AS JSON), :created_at,
                        NULL, NULL, NULL, NULL, 0, 'worker-test', :key,
                        :fingerprint, NULL, NULL, NULL, NULL, 1
                    )
                    """
                ),
                {
                    "id": job_id,
                    "config": ('{"candidate_fps":5.0,"selection_window_seconds":1.0}'),
                    "created_at": now,
                    "key": str(uuid4()),
                    "fingerprint": "f" * 64,
                },
            )
        claim = repository.claim(job_id, 60)
        assert claim.job is not None
        assert claim.job.status == JobStatus.RUNNING
        assert claim.job.attempt_count == 1
    finally:
        with repository._engine.begin() as connection:
            connection.execute(
                text("DELETE FROM processing_jobs WHERE id = :id"), {"id": job_id}
            )
        repository.close()
