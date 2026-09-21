"""Real PostgreSQL checks for durable inference dispatch."""

import asyncio
import os
import random
import sys
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_annotation_migration import remove_annotation_project, seed_annotation_project

from app.models.annotation_inference import (
    AnnotationInferenceOutbox,
    AnnotationInferenceRun,
)
from app.models.annotation_training import AnnotationTrainingRun
from app.models.annotations import AnnotationClass, AnnotationImage, AnnotationProject
from app.outbox.repository import OutboxRepository
from app.services.annotation_inference import AnnotationInferenceService


class InferencePublisher:
    def __init__(self) -> None:
        self.calls = []
        self.fail = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False

    def publish_inference(self, inference_id):
        self.started.set()
        if self.block:
            assert self.release.wait(5)
        if self.fail:
            raise ConnectionError("broker unavailable")
        self.calls.append(inference_id)


def run_async(coroutine):
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)


async def exercise_dispatch(project_id, training_id):
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    run_id = uuid4()
    event_id = uuid4()
    try:
        rollback_run = uuid4()
        rollback_event = uuid4()
        async with sessions() as session:
            session.add(
                AnnotationInferenceRun(
                    id=rollback_run,
                    project_id=project_id,
                    training_id=training_id,
                    model_version=1,
                    status="PENDING",
                    targets=[{"image_index": 0}],
                    target_image_count=1,
                    processed_image_count=0,
                    created_box_count=0,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                AnnotationInferenceOutbox(
                    id=rollback_event,
                    inference_id=rollback_run,
                    created_at=now,
                    next_attempt_at=now,
                    attempt_count=0,
                )
            )
            await session.flush()
            await session.rollback()
        async with sessions() as session:
            assert await session.get(AnnotationInferenceRun, rollback_run) is None
            assert await session.get(AnnotationInferenceOutbox, rollback_event) is None

        async with sessions() as session:
            session.add(
                AnnotationInferenceRun(
                    id=run_id,
                    project_id=project_id,
                    training_id=training_id,
                    model_version=1,
                    status="PENDING",
                    targets=[{"image_index": 0}],
                    target_image_count=1,
                    processed_image_count=0,
                    created_box_count=0,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                AnnotationInferenceOutbox(
                    id=event_id,
                    inference_id=run_id,
                    created_at=now,
                    next_attempt_at=now,
                    attempt_count=0,
                )
            )
            await session.commit()

        async with sessions() as session:
            assert await session.get(AnnotationInferenceRun, run_id)
            assert await session.get(AnnotationInferenceOutbox, event_id)
            duplicate = AnnotationInferenceOutbox(
                id=uuid4(),
                inference_id=run_id,
                created_at=now,
                next_attempt_at=now,
                attempt_count=0,
            )
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

        repository = OutboxRepository(
            sessions,
            backoff_base_seconds=1,
            backoff_max_seconds=1,
            random_source=random.Random(1),
        )
        publisher = InferencePublisher()
        publisher.fail = True
        assert await repository.publish_ready(publisher, 1) == 1
        async with sessions() as session:
            event = await session.get(AnnotationInferenceOutbox, event_id)
            assert event.published_at is None
            assert event.attempt_count == 1
            assert event.next_attempt_at > now

        async with sessions() as session:
            event = await session.get(AnnotationInferenceOutbox, event_id)
            event.next_attempt_at = now
            await session.commit()
        publisher.fail = False
        publisher.block = True
        publisher.started.clear()
        first = asyncio.create_task(repository.publish_ready(publisher, 1))
        assert await asyncio.to_thread(publisher.started.wait, 5)
        contender = InferencePublisher()
        assert await repository.publish_ready(contender, 1) == 0
        publisher.release.set()
        assert await first == 1
        assert publisher.calls == [run_id]
        assert contender.calls == []
        assert await repository.publish_ready(contender, 1) == 0
        async with sessions() as session:
            event = await session.get(AnnotationInferenceOutbox, event_id)
            assert event.published_at is not None
            assert event.attempt_count == 2
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AnnotationInferenceOutbox)
                    .where(AnnotationInferenceOutbox.inference_id == run_id)
                )
                == 1
            )
    finally:
        async with sessions() as session:
            await session.execute(
                sa.delete(AnnotationInferenceRun).where(
                    AnnotationInferenceRun.id == run_id
                )
            )
            await session.commit()
        await engine.dispose()


def test_inference_outbox_transaction_retry_and_parallel_publish():
    job_id, project_id, training_id = run_async(seed_annotation_project())
    try:
        run_async(exercise_dispatch(project_id, training_id))
    finally:
        assert set(
            run_async(remove_annotation_project(job_id, project_id)).values()
        ) == {0}


async def exercise_production_start(job_id, project_id, training_id):
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    class_id = uuid4()
    try:
        async with sessions() as session:
            project = await session.get(AnnotationProject, project_id)
            session.add(
                AnnotationClass(
                    id=class_id,
                    project_id=project_id,
                    yolo_index=0,
                    name="vehicle",
                    normalized_name="vehicle",
                    color="#FF0000",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AnnotationImage(
                    project_id=project_id,
                    image_index=0,
                    image_filename="frame.jpg",
                    image_sha256="c" * 64,
                    yolo_sha256="c" * 64,
                    completed=False,
                    updated_at=now,
                )
            )
            await session.execute(
                sa.text(
                    "UPDATE annotation_training_snapshot_classes "
                    "SET class_id = :class_id "
                    "WHERE training_id = :training_id"
                ),
                {"class_id": class_id, "training_id": training_id},
            )
            await session.execute(
                sa.text(
                    "UPDATE annotation_training_runs "
                    "SET status = 'SUCCEEDED', model_version = 1, "
                    "snapshot_artifact_reference = 's3://test/snapshot', "
                    "snapshot_artifact_size_bytes = 1, "
                    "snapshot_artifact_sha256 = :sha, "
                    "model_artifact_reference = 's3://test/model', "
                    "model_artifact_size_bytes = 1, model_artifact_sha256 = :sha "
                    "WHERE id = :training_id"
                ),
                {"sha": "a" * 64, "training_id": training_id},
            )
            await session.commit()

        manifest = SimpleNamespace(
            run_token=project.result_run_token,
            frames=[
                SimpleNamespace(
                    index=0,
                    filename="frame.jpg",
                    object_key="jobs/test/frame.jpg",
                    size_bytes=100,
                    content_type="image/jpeg",
                    sha256="c" * 64,
                    width=640,
                    height=360,
                )
            ],
        )

        class Results:
            async def annotation_source(self, requested_job):
                assert requested_job == job_id
                return None, manifest

        async with sessions() as session:
            service = AnnotationInferenceService(session, Results())
            injected = False

            def fail_outbox(_conn, _cursor, statement, _parameters, _context, _many):
                nonlocal injected
                if not injected and statement.startswith(
                    "INSERT INTO annotation_inference_outbox"
                ):
                    injected = True
                    raise RuntimeError("injected outbox failure")

            sa.event.listen(engine.sync_engine, "before_cursor_execute", fail_outbox)
            try:
                with pytest.raises(RuntimeError, match="injected outbox failure"):
                    await service.start(job_id)
            finally:
                sa.event.remove(
                    engine.sync_engine, "before_cursor_execute", fail_outbox
                )
            assert injected
            async with sessions() as verify:
                assert (
                    await verify.scalar(
                        sa.select(sa.func.count())
                        .select_from(AnnotationInferenceRun)
                        .where(AnnotationInferenceRun.project_id == project_id)
                    )
                    == 0
                )
                assert (
                    await verify.scalar(
                        sa.select(sa.func.count()).select_from(
                            AnnotationInferenceOutbox
                        )
                    )
                    == 0
                )

            response, created = await service.start(job_id)
            assert created
            again, created_again = await service.start(job_id)
            assert not created_again and again.id == response.id

        async with sessions() as verify:
            run = await verify.get(AnnotationInferenceRun, response.id)
            event = await verify.scalar(
                sa.select(AnnotationInferenceOutbox).where(
                    AnnotationInferenceOutbox.inference_id == response.id
                )
            )
            assert run is not None and run.training_id == training_id
            assert run.targets[0]["source_sha256"] == "c" * 64
            assert run.targets[0]["source_object_key"] == "jobs/test/frame.jpg"
            training = await verify.get(AnnotationTrainingRun, run.training_id)
            assert training.request_fingerprint == "b" * 64
            assert event is not None and event.inference_id == run.id
            assert event.published_at is None
            assert (
                await verify.scalar(
                    sa.select(sa.func.count())
                    .select_from(AnnotationInferenceOutbox)
                    .where(AnnotationInferenceOutbox.inference_id == response.id)
                )
                == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL") is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
def test_production_inference_start_atomic_rollback_and_retry():
    job_id, project_id, training_id = run_async(seed_annotation_project())
    try:
        run_async(exercise_production_start(job_id, project_id, training_id))
    finally:
        assert set(
            run_async(remove_annotation_project(job_id, project_id)).values()
        ) == {0}
