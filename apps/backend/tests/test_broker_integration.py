import asyncio
import os
import sys
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from celery import Celery
from celery.contrib.testing.worker import start_worker
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.jobs import JobStatus, OutboxEventType, SourceType
from app.models.job_outbox import JobOutbox
from app.models.processing_job import ProcessingJob
from app.outbox.celery_client import CeleryJobMessagePublisher
from app.outbox.repository import OutboxRepository
from app.repositories.jobs import SQLAlchemyJobRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("BROKER_INTEGRATION") != "1"
    or not os.environ.get("TEST_DATABASE_URL"),
    reason="Real PostgreSQL and Redis integration is not enabled",
)


def test_postgres_outbox_delivers_minimal_payload_through_redis() -> None:
    _run(_exercise_delivery())


async def _exercise_delivery() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    job_id = uuid4()
    event_id = uuid4()
    received = threading.Event()
    delivery = {}
    worker_app = Celery("broker-integration", broker=broker_url)
    worker_app.conf.update(task_serializer="json", accept_content=["json"])

    @worker_app.task(name="frame_worker.process_video", bind=True)
    def capture(self, job_id: str) -> None:
        delivery["args"] = self.request.args
        delivery["kwargs"] = self.request.kwargs
        delivery["job_id"] = job_id
        received.set()

    job = ProcessingJob(
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
        idempotency_scope="broker-test",
        idempotency_key=str(uuid4()),
        request_fingerprint="f" * 64,
        result_reference=None,
        result_summary=None,
        run_token=None,
        lease_expires_at=None,
        version=1,
    )
    event = JobOutbox(
        id=event_id,
        aggregate_id=job_id,
        event_type=OutboxEventType.PROCESS_VIDEO_JOB,
        payload={"job_id": str(job_id)},
        created_at=now,
        published_at=None,
        attempt_count=0,
        next_attempt_at=now,
    )
    try:
        async with sessions() as session:
            await SQLAlchemyJobRepository(session).create_with_outbox(job, event)
        repository = OutboxRepository(
            sessions,
            backoff_base_seconds=1,
            backoff_max_seconds=2,
        )
        client = CeleryJobMessagePublisher(broker_url)
        try:
            with start_worker(
                worker_app,
                pool="solo",
                queues=("video-processing",),
                perform_ping_check=False,
            ):
                assert await repository.publish_ready(client, 1) == 1
                assert await asyncio.to_thread(received.wait, 10)
        finally:
            client.close()
        assert delivery == {
            "args": [],
            "kwargs": {"job_id": str(job_id)},
            "job_id": str(job_id),
        }
    finally:
        async with sessions() as session:
            await session.execute(sa.delete(JobOutbox).where(JobOutbox.id == event_id))
            await session.execute(
                sa.delete(ProcessingJob).where(ProcessingJob.id == job_id)
            )
            await session.commit()
        await engine.dispose()


def _run(coroutine) -> None:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(coroutine)
        return
    asyncio.run(coroutine)
