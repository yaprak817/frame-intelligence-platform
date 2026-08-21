import asyncio
import os
import sys
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.dependencies import get_job_service
from app.domain.jobs import JobStatus, OutboxEventType, SourceType
from app.main import app
from app.models.job_outbox import JobOutbox
from app.models.processing_job import ProcessingJob
from app.repositories.jobs import SQLAlchemyJobRepository
from app.security.source_secrets import SourceSecretCipher

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)


def _job_and_event() -> tuple[ProcessingJob, JobOutbox]:
    job_id = uuid4()
    now = datetime.now(UTC)
    job = ProcessingJob(
        id=job_id,
        status=JobStatus.PENDING_DISPATCH,
        source_type=SourceType.URL,
        source_display="https://example.com/video",
        source_secret="protected-source",
        processing_config={"candidate_fps": 5.0},
        created_at=now,
        started_at=None,
        completed_at=None,
        failure_code=None,
        failure_message=None,
        attempt_count=0,
        idempotency_scope="POST:/api/v1/jobs/url",
        idempotency_key=f"repository-{uuid4()}",
        request_fingerprint="f" * 64,
        result_reference=None,
        result_summary=None,
        run_token=None,
        lease_expires_at=None,
        version=1,
    )
    event = JobOutbox(
        id=uuid4(),
        aggregate_id=job_id,
        event_type=OutboxEventType.PROCESS_VIDEO_JOB,
        payload={"job_id": str(job_id)},
        created_at=now,
        published_at=None,
        attempt_count=0,
        next_attempt_at=now,
    )
    return job, event


async def _run_repository_checks() -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    job, event = _job_and_event()
    try:
        async with sessions() as session:
            repository = SQLAlchemyJobRepository(session)
            await repository.create_with_outbox(job, event)
        async with sessions() as session:
            repository = SQLAlchemyJobRepository(session)
            persisted = await repository.get(job.id)
            idempotent = await repository.find_by_idempotency(
                job.idempotency_scope, job.idempotency_key
            )
            outbox_payload = await session.scalar(
                sa.select(JobOutbox.payload).where(JobOutbox.aggregate_id == job.id)
            )
        assert persisted is not None
        assert persisted.status == JobStatus.PENDING_DISPATCH
        assert idempotent is not None and idempotent.id == job.id
        assert outbox_payload == {"job_id": str(job.id)}
    finally:
        async with sessions() as session:
            await session.execute(
                sa.delete(JobOutbox).where(JobOutbox.aggregate_id == job.id)
            )
            await session.execute(
                sa.delete(ProcessingJob).where(ProcessingJob.id == job.id)
            )
            await session.commit()
        await engine.dispose()


async def _run_rollback_check() -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    job, event = _job_and_event()
    event.event_type = None  # type: ignore[assignment]
    try:
        async with sessions() as session:
            repository = SQLAlchemyJobRepository(session)
            with pytest.raises(sa.exc.IntegrityError):
                await repository.create_with_outbox(job, event)
        async with sessions() as session:
            assert await session.get(ProcessingJob, job.id) is None
            assert await session.get(JobOutbox, event.id) is None
    finally:
        await engine.dispose()


async def _inspect_api_job(job_id: object) -> tuple[ProcessingJob, JobOutbox]:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            job = await session.get(ProcessingJob, job_id)
            event = await session.scalar(
                sa.select(JobOutbox).where(JobOutbox.aggregate_id == job_id)
            )
            assert job is not None
            assert event is not None
            return job, event
    finally:
        await engine.dispose()


async def _delete_api_job(job_id: object) -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            await session.execute(
                sa.delete(JobOutbox).where(JobOutbox.aggregate_id == job_id)
            )
            await session.execute(
                sa.delete(ProcessingJob).where(ProcessingJob.id == job_id)
            )
            await session.commit()
    finally:
        await engine.dispose()


def test_repository_persists_job_and_outbox() -> None:
    _run(_run_repository_checks())


def test_outbox_failure_rolls_back_job() -> None:
    _run(_run_rollback_check())


def test_real_database_api_encryption_and_idempotency(caplog) -> None:
    raw_url = "https://example.com/video?id=123&token=VERY_SECRET_TOKEN"
    idempotency_key = f"postgres-api-{uuid4()}"
    headers = {"Idempotency-Key": idempotency_key}
    app.dependency_overrides.pop(get_job_service, None)

    with TestClient(
        app,
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
    ) as client:
        first = client.post("/api/v1/jobs/url", headers=headers, json={"url": raw_url})
        repeated = client.post(
            "/api/v1/jobs/url", headers=headers, json={"url": raw_url}
        )
        conflicting = client.post(
            "/api/v1/jobs/url",
            headers=headers,
            json={"url": "https://example.com/different"},
        )
        status_response = client.get(first.headers["location"])

    assert first.status_code == 202
    assert first.headers["location"] == first.json()["status_url"]
    assert first.json()["status"] == "PENDING_DISPATCH"
    job_id = UUID(first.json()["job_id"])
    assert repeated.status_code == 202
    assert repeated.json()["job_id"] == str(job_id)
    assert conflicting.status_code == 409
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "PENDING_DISPATCH"
    assert status_response.json()["source"] == "https://example.com/video"
    for output in (first.text, repeated.text, conflicting.text, status_response.text):
        assert raw_url not in output
        assert "VERY_SECRET_TOKEN" not in output
    assert "VERY_SECRET_TOKEN" not in caplog.text

    job, event = _run_result(_inspect_api_job(job_id))
    try:
        assert raw_url not in job.source_secret
        cipher = SourceSecretCipher(os.environ["JOB_SOURCE_ENCRYPTION_KEY"])
        assert cipher.decrypt(job.source_secret, job.id) == raw_url
        assert event.payload == {"job_id": str(job.id)}
        assert raw_url not in str(event.payload)
        assert "processing_config" not in event.payload
    finally:
        _run(_delete_api_job(job_id))


def _run(coroutine: object) -> None:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(coroutine)  # type: ignore[arg-type]
        return
    asyncio.run(coroutine)  # type: ignore[arg-type]


def _run_result(coroutine: object) -> object:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coroutine)  # type: ignore[arg-type]
    return asyncio.run(coroutine)  # type: ignore[arg-type]
