import asyncio
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.jobs import JobStatus, SourceType
from app.models.annotation_training import (
    AnnotationTrainingOutbox,
    AnnotationTrainingRun,
    AnnotationTrainingSnapshotBox,
    AnnotationTrainingSnapshotClass,
    AnnotationTrainingSnapshotImage,
)
from app.models.annotations import (
    AnnotationBox,
    AnnotationClass,
    AnnotationImage,
    AnnotationProject,
)
from app.models.processing_job import ProcessingJob
from app.schemas.annotation_training import CreateAnnotationTrainingRequest
from app.schemas.artifacts import StoredManifestV1
from app.services.annotation_training import (
    AnnotationTrainingInsufficientImages,
    AnnotationTrainingInvalidDataset,
    AnnotationTrainingRevisionAlreadySnapshotted,
    AnnotationTrainingService,
    AnnotationTrainingSnapshotLimitReached,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)


class FakeResults:
    def __init__(self, job, manifest) -> None:
        self.job = job
        self.manifest = manifest

    async def annotation_source(self, job_id):
        assert job_id == self.job.id
        return self.job, self.manifest


def test_postgres_integration_uses_canonical_psycopg_driver() -> None:
    assert TEST_DATABASE_URL is not None
    assert sa.engine.make_url(TEST_DATABASE_URL).drivername == "postgresql+psycopg"


def stored_manifest(job_id, run_token, count: int) -> StoredManifestV1:
    return StoredManifestV1.model_validate(
        {
            "schema_version": 1,
            "job_id": job_id,
            "run_token": run_token,
            "created_at": datetime.now(UTC),
            "summary": {
                "frames_saved": count,
                "candidates": count,
                "shortlisted": count,
                "duplicates_removed": 0,
                "processing_seconds": 1,
                "duration_seconds": count,
            },
            "frames": [
                {
                    "index": index,
                    "filename": f"frame_{index:06d}_{index}ms_640x360.jpg",
                    "object_key": (
                        f"jobs/{job_id}/results/{run_token}/frames/"
                        f"frame_{index:06d}_{index}ms_640x360.jpg"
                    ),
                    "content_type": "image/jpeg",
                    "size_bytes": 100 + index,
                    "sha256": f"{index + 1:064x}",
                    "timestamp_ms": index,
                    "width": 640,
                    "height": 360,
                }
                for index in range(count)
            ],
        }
    )


async def exercise_snapshot_foundation() -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    job_id, project_id, run_token, class_id = uuid4(), uuid4(), uuid4(), uuid4()
    unused_class_id = uuid4()
    now = datetime.now(UTC)
    job = ProcessingJob(
        id=job_id,
        status=JobStatus.SUCCEEDED,
        source_type=SourceType.UPLOAD,
        source_display="snapshot.mp4",
        source_secret=None,
        source_reference={"object_key": "safe"},
        processing_config={},
        created_at=now,
        started_at=now,
        completed_at=now,
        failure_code=None,
        failure_message=None,
        attempt_count=1,
        idempotency_scope="snapshot-test",
        idempotency_key=str(uuid4()),
        request_fingerprint="f" * 64,
        result_reference=(
            f"s3://frame-intelligence/jobs/{job_id}/results/{run_token}/manifest.json"
        ),
        result_summary={},
        run_token=None,
        lease_expires_at=None,
        version=1,
    )
    manifest = stored_manifest(job_id, run_token, 201)
    results = FakeResults(job, manifest)
    try:
        async with sessions() as session:
            session.add(job)
            await session.flush()
            session.add(
                AnnotationProject(
                    id=project_id,
                    job_id=job_id,
                    result_run_token=run_token,
                    revision=3,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                AnnotationClass(
                    id=class_id,
                    project_id=project_id,
                    yolo_index=0,
                    name="vehicle",
                    normalized_name="vehicle",
                    color="#123456",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AnnotationClass(
                    id=unused_class_id,
                    project_id=project_id,
                    yolo_index=7,
                    name="unused",
                    normalized_name="unused",
                    color="#654321",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add_all(
                [
                    AnnotationImage(
                        project_id=project_id,
                        image_index=index,
                        image_filename=manifest.frames[index].filename,
                        image_sha256=manifest.frames[index].sha256,
                        yolo_sha256=manifest.frames[index].sha256,
                        completed=True,
                        updated_at=now,
                    )
                    for index in range(201)
                ]
            )
            await session.flush()
            session.add_all(
                [
                    AnnotationBox(
                        id=uuid4(),
                        project_id=project_id,
                        image_index=index,
                        class_id=class_id,
                        x_center=Decimal("0.50000000"),
                        y_center=Decimal("0.50000000"),
                        width=Decimal("0.25000000"),
                        height=Decimal("0.25000000"),
                        created_at=now,
                        updated_at=now,
                    )
                    for index in range(49)
                ]
            )
            await session.commit()

        request = CreateAnnotationTrainingRequest.model_validate(
            {"expected_revision": 3, "config": {"max_snapshot_images": 50}}
        )
        async with sessions() as left, sessions() as right:
            first, second = await asyncio.gather(
                AnnotationTrainingService(left, results).create(
                    job_id, request, "snapshot-concurrent-0001"
                ),
                AnnotationTrainingService(right, results).create(
                    job_id, request, "snapshot-concurrent-0001"
                ),
            )
        assert sum(created for _response, created in (first, second)) == 1
        assert first[0].id == second[0].id
        assert first[0].image_count == 50
        assert first[0].box_count == 49
        assert first[0].source_revision == 3
        assert first[0].train_image_count == 40
        assert first[0].validation_image_count == 10

        async with sessions() as left, sessions() as right:
            started = await asyncio.gather(
                AnnotationTrainingService(left, results).start(job_id, first[0].id),
                AnnotationTrainingService(right, results).start(job_id, first[0].id),
            )
        assert sum(dispatched for _response, dispatched in started) == 1
        async with sessions() as session:
            assert (
                await session.scalar(
                    sa.select(sa.func.count()).select_from(AnnotationTrainingOutbox)
                )
                == 1
            )
            await session.execute(sa.delete(AnnotationTrainingOutbox))
            await session.execute(
                sa.update(AnnotationTrainingRun)
                .where(AnnotationTrainingRun.id == first[0].id)
                .values(status="SNAPSHOT_READY", progress_total=0)
            )
            await session.commit()

        request_200 = CreateAnnotationTrainingRequest.model_validate(
            {"expected_revision": 3, "config": {"max_snapshot_images": 200}}
        )
        async with sessions() as session:
            with pytest.raises(AnnotationTrainingRevisionAlreadySnapshotted):
                await AnnotationTrainingService(session, results).create(
                    job_id, request_200, "snapshot-bounded-0200-conflict"
                )
            await session.execute(
                sa.update(AnnotationProject)
                .where(AnnotationProject.id == project_id)
                .values(revision=4)
            )
            await session.commit()
        request_200 = CreateAnnotationTrainingRequest.model_validate(
            {"expected_revision": 4, "config": {"max_snapshot_images": 200}}
        )
        async with sessions() as session:
            bounded_200, created_200 = await AnnotationTrainingService(
                session, results
            ).create(job_id, request_200, "snapshot-bounded-0200")
        assert created_200
        assert bounded_200.snapshot_version == 2
        assert bounded_200.image_count == 200
        assert bounded_200.train_image_count == 160
        assert bounded_200.validation_image_count == 40

        for revision in (5, 6):
            async with sessions() as session:
                await session.execute(
                    sa.update(AnnotationProject)
                    .where(AnnotationProject.id == project_id)
                    .values(revision=revision)
                )
                await session.commit()
            revision_request = CreateAnnotationTrainingRequest.model_validate(
                {"expected_revision": revision, "config": {"max_snapshot_images": 50}}
            )
            async with sessions() as session:
                _item, created = await AnnotationTrainingService(
                    session, results
                ).create(job_id, revision_request, f"snapshot-revision-{revision}")
            assert created

        async with sessions() as session:
            await session.execute(
                sa.update(AnnotationProject)
                .where(AnnotationProject.id == project_id)
                .values(revision=7)
            )
            await session.commit()
        boundary_request = CreateAnnotationTrainingRequest.model_validate(
            {"expected_revision": 7, "config": {"max_snapshot_images": 50}}
        )
        async with sessions() as left, sessions() as right:
            boundary = await asyncio.gather(
                AnnotationTrainingService(left, results).create(
                    job_id, boundary_request, "snapshot-boundary-left"
                ),
                AnnotationTrainingService(right, results).create(
                    job_id, boundary_request, "snapshot-boundary-right"
                ),
                return_exceptions=True,
            )
        assert sum(isinstance(item, tuple) and item[1] for item in boundary) == 1
        assert any(
            isinstance(item, AnnotationTrainingRevisionAlreadySnapshotted)
            for item in boundary
        )
        async with sessions() as session:
            training_service = AnnotationTrainingService(session, results)
            first_page = await training_service.list_runs(
                job_id, limit=2, after_snapshot_version=None
            )
            second_page = await training_service.list_runs(
                job_id,
                limit=2,
                after_snapshot_version=first_page.next_cursor,
            )
            last_page = await training_service.list_runs(
                job_id,
                limit=2,
                after_snapshot_version=second_page.next_cursor,
            )
        versions = [
            item.snapshot_version
            for page in (first_page, second_page, last_page)
            for item in page.items
        ]
        assert versions == [1, 2, 3, 4, 5]
        assert len(versions) == len(set(versions))
        assert first_page.has_more and second_page.has_more
        assert not last_page.has_more and last_page.next_cursor is None

        async with sessions() as session:
            await session.execute(
                sa.update(AnnotationProject)
                .where(AnnotationProject.id == project_id)
                .values(revision=8)
            )
            await session.commit()
        sixth_request = CreateAnnotationTrainingRequest.model_validate(
            {"expected_revision": 8, "config": {"max_snapshot_images": 50}}
        )
        async with sessions() as session:
            with pytest.raises(AnnotationTrainingSnapshotLimitReached):
                await AnnotationTrainingService(session, results).create(
                    job_id, sixth_request, "snapshot-limit-sixth"
                )
            replay, replay_created = await AnnotationTrainingService(
                session, results
            ).create(job_id, request, "snapshot-concurrent-0001")
        assert not replay_created and replay.id == first[0].id

        async with sessions() as session:
            await session.execute(
                sa.update(AnnotationImage)
                .where(
                    AnnotationImage.project_id == project_id,
                    AnnotationImage.image_index >= 49,
                )
                .values(completed=False)
            )
            await session.commit()
        request_52 = CreateAnnotationTrainingRequest.model_validate(
            {"expected_revision": 8, "config": {"max_snapshot_images": 52}}
        )
        async with sessions() as session:
            with pytest.raises(AnnotationTrainingInsufficientImages):
                await AnnotationTrainingService(
                    session, results, max_snapshots_per_project=20
                ).create(job_id, request_52, "snapshot-insufficient-0049")
            assert await session.scalar(sa.select(sa.literal(1))) == 1
            await session.execute(
                sa.update(AnnotationImage)
                .where(AnnotationImage.project_id == project_id)
                .values(completed=True)
            )
            await session.commit()

        original_size = manifest.frames[0].size_bytes
        manifest.frames[0].size_bytes = 0
        request_51 = CreateAnnotationTrainingRequest.model_validate(
            {"expected_revision": 8, "config": {"max_snapshot_images": 51}}
        )
        async with sessions() as session:
            with pytest.raises(sa.exc.IntegrityError):
                await AnnotationTrainingService(
                    session, results, max_snapshots_per_project=20
                ).create(job_id, request_51, "snapshot-rollback-0051")
            assert await session.scalar(sa.select(sa.literal(1))) == 1
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AnnotationTrainingRun)
                    .where(AnnotationTrainingRun.project_id == project_id)
                )
                == 5
            )
        manifest.frames[0].size_bytes = original_size

        async with sessions() as session:
            await session.execute(
                sa.delete(AnnotationBox).where(AnnotationBox.project_id == project_id)
            )
            await session.commit()
        request_53 = CreateAnnotationTrainingRequest.model_validate(
            {"expected_revision": 8, "config": {"max_snapshot_images": 53}}
        )
        async with sessions() as session:
            with pytest.raises(AnnotationTrainingInvalidDataset):
                await AnnotationTrainingService(
                    session, results, max_snapshots_per_project=20
                ).create(job_id, request_53, "snapshot-zero-box-0053")
        async with sessions() as session:
            annotation_class = await session.get(AnnotationClass, class_id)
            assert annotation_class is not None
            await session.delete(annotation_class)
            await session.commit()
        request_54 = CreateAnnotationTrainingRequest.model_validate(
            {"expected_revision": 8, "config": {"max_snapshot_images": 54}}
        )
        async with sessions() as session:
            with pytest.raises(AnnotationTrainingInvalidDataset):
                await AnnotationTrainingService(
                    session, results, max_snapshots_per_project=20
                ).create(job_id, request_54, "snapshot-zero-class-0054")

        def pending(version: int) -> AnnotationTrainingRun:
            return AnnotationTrainingRun(
                id=uuid4(),
                project_id=project_id,
                snapshot_version=version,
                source_revision=100 + version,
                status="PENDING",
                selected_image_count=50,
                selected_class_count=1,
                selected_box_count=1,
                train_image_count=40,
                validation_image_count=10,
                config={"max_snapshot_images": 50},
                config_hash=f"{version:064x}",
                idempotency_key=f"active-training-{version}",
                request_fingerprint=f"{version + 100:064x}",
                created_at=now,
                started_at=None,
                completed_at=None,
                failure_code=None,
                snapshot_artifact_reference=None,
                snapshot_artifact_size_bytes=None,
                snapshot_artifact_sha256=None,
                model_artifact_reference=None,
                model_artifact_size_bytes=None,
                model_artifact_sha256=None,
            )

        async with sessions() as session:
            active = pending(20)
            session.add(active)
            await session.commit()
        async with sessions() as session:
            session.add(pending(21))
            with pytest.raises(sa.exc.IntegrityError):
                await session.commit()
            await session.rollback()
            assert await session.scalar(sa.select(sa.literal(1))) == 1
            await session.delete(await session.get(AnnotationTrainingRun, active.id))
            await session.commit()

        async with sessions() as session:
            run = await session.get(AnnotationTrainingRun, first[0].id)
            assert run is not None and run.snapshot_version == 1
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AnnotationTrainingSnapshotClass)
                    .where(AnnotationTrainingSnapshotClass.training_id == first[0].id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AnnotationTrainingSnapshotImage)
                    .where(AnnotationTrainingSnapshotImage.training_id == first[0].id)
                )
                == 50
            )
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AnnotationTrainingSnapshotBox)
                    .where(AnnotationTrainingSnapshotBox.training_id == first[0].id)
                )
                == 49
            )
            negative = await session.get(
                AnnotationTrainingSnapshotImage, (first[0].id, 49)
            )
            assert negative is not None
            project = await session.get(AnnotationProject, project_id)
            assert project is not None and project.revision == 8

        async with sessions() as session:
            frozen_class = await session.get(
                AnnotationTrainingSnapshotClass, (first[0].id, 0)
            )
            frozen_boxes = await session.scalar(
                sa.select(sa.func.count())
                .select_from(AnnotationTrainingSnapshotBox)
                .where(AnnotationTrainingSnapshotBox.training_id == first[0].id)
            )
            assert frozen_class is not None and frozen_class.name == "vehicle"
            assert frozen_boxes == 49
    finally:
        async with sessions() as session:
            await session.execute(
                sa.delete(ProcessingJob).where(ProcessingJob.id == job_id)
            )
            await session.commit()
        await engine.dispose()


def test_real_postgres_snapshot_is_transactional_idempotent_and_immutable() -> None:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(exercise_snapshot_foundation())
    else:
        asyncio.run(exercise_snapshot_foundation())
