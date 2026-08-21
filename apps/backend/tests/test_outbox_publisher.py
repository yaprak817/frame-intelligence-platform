import asyncio
import os
import random
import sys
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.jobs import JobStatus, OutboxEventType, SourceType
from app.models.job_outbox import JobOutbox
from app.models.processing_job import ProcessingJob
from app.outbox.repository import (
    InvalidOutboxPayloadError,
    OutboxRepository,
    parse_job_id,
)
from app.repositories.jobs import SQLAlchemyJobRepository


class RecordingPublisher:
    def __init__(self, fail: bool = False) -> None:
        self.jobs = []
        self.fail = fail

    def publish(self, job_id) -> None:
        if self.fail:
            raise ConnectionError("redis://user:secret@broker")
        self.jobs.append(job_id)


class BlockingPublisher(RecordingPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def publish(self, job_id) -> None:
        self.started.set()
        assert self.release.wait(5)
        super().publish(job_id)


def test_payload_accepts_only_job_id() -> None:
    job_id = uuid4()
    assert parse_job_id({"job_id": str(job_id)}) == job_id
    with pytest.raises(InvalidOutboxPayloadError):
        parse_job_id({"job_id": str(job_id), "url": "https://secret.invalid"})
    with pytest.raises(InvalidOutboxPayloadError):
        parse_job_id({"job_id": "invalid"})


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
postgres = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)


def _records() -> tuple[ProcessingJob, JobOutbox]:
    now = datetime.now(UTC)
    job_id = uuid4()
    return (
        ProcessingJob(
            id=job_id,
            status=JobStatus.PENDING_DISPATCH,
            source_type=SourceType.URL,
            source_display="https://example.com/video",
            source_secret="protected",
            source_reference=None,
            processing_config={
                "candidate_fps": 5.0,
                "selection_window_seconds": 1.0,
            },
            created_at=now,
            started_at=None,
            completed_at=None,
            failure_code=None,
            failure_message=None,
            attempt_count=0,
            idempotency_scope="test",
            idempotency_key=str(uuid4()),
            request_fingerprint="f" * 64,
            result_reference=None,
            result_summary=None,
            run_token=None,
            lease_expires_at=None,
            version=1,
        ),
        JobOutbox(
            id=uuid4(),
            aggregate_id=job_id,
            event_type=OutboxEventType.PROCESS_VIDEO_JOB,
            payload={"job_id": str(job_id)},
            created_at=now,
            published_at=None,
            attempt_count=0,
            next_attempt_at=now,
        ),
    )


async def _exercise_publisher(fail: bool) -> None:
    assert TEST_DATABASE_URL
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    job, event = _records()
    try:
        async with sessions() as session:
            await SQLAlchemyJobRepository(session).create_with_outbox(job, event)
        repository = OutboxRepository(
            sessions,
            backoff_base_seconds=10,
            backoff_max_seconds=10,
            random_source=random.Random(1),
        )
        publisher = RecordingPublisher(fail=fail)
        assert await repository.publish_ready(publisher, 1) == 1
        async with sessions() as session:
            stored_job = await session.get(ProcessingJob, job.id)
            stored_event = await session.get(JobOutbox, event.id)
            assert stored_job and stored_event
            if fail:
                assert stored_job.status == JobStatus.PENDING_DISPATCH
                assert stored_event.published_at is None
                assert stored_event.next_attempt_at > stored_event.created_at
            else:
                assert publisher.jobs == [job.id]
                assert stored_job.status == JobStatus.QUEUED
                assert stored_event.published_at is not None
            assert stored_event.attempt_count == 1
    finally:
        async with sessions() as session:
            await session.execute(sa.delete(JobOutbox).where(JobOutbox.id == event.id))
            await session.execute(
                sa.delete(ProcessingJob).where(ProcessingJob.id == job.id)
            )
            await session.commit()
        await engine.dispose()


@postgres
def test_publish_success_is_atomic() -> None:
    _run(_exercise_publisher(False))


@postgres
def test_broker_failure_keeps_job_pending(caplog) -> None:
    _run(_exercise_publisher(True))
    assert "redis://" not in caplog.text
    assert "secret" not in caplog.text


async def _exercise_skip_locked() -> None:
    assert TEST_DATABASE_URL
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    job, event = _records()
    publisher = BlockingPublisher()
    try:
        async with sessions() as session:
            await SQLAlchemyJobRepository(session).create_with_outbox(job, event)
        first_repository = OutboxRepository(
            sessions, backoff_base_seconds=1, backoff_max_seconds=2
        )
        second_repository = OutboxRepository(
            sessions, backoff_base_seconds=1, backoff_max_seconds=2
        )
        first = asyncio.create_task(first_repository.publish_ready(publisher, 1))
        assert await asyncio.to_thread(publisher.started.wait, 5)
        assert await second_repository.publish_ready(RecordingPublisher(), 1) == 0
        publisher.release.set()
        assert await first == 1
        assert publisher.jobs == [job.id]
    finally:
        publisher.release.set()
        async with sessions() as session:
            await session.execute(sa.delete(JobOutbox).where(JobOutbox.id == event.id))
            await session.execute(
                sa.delete(ProcessingJob).where(ProcessingJob.id == job.id)
            )
            await session.commit()
        await engine.dispose()


@postgres
def test_skip_locked_prevents_parallel_publish() -> None:
    _run(_exercise_skip_locked())


def _run(coroutine) -> None:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(coroutine)
        return
    asyncio.run(coroutine)
