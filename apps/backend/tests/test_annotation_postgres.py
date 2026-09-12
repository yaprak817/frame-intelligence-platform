import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.jobs import JobStatus, SourceType
from app.models.annotations import (
    AnnotationBox,
    AnnotationClass,
    AnnotationImage,
    AnnotationProject,
)
from app.models.processing_job import ProcessingJob
from app.schemas.annotations import (
    CreateAnnotationClassRequest,
    PutImageAnnotationsRequest,
)
from app.schemas.artifacts import StoredDatasetManifestV1
from app.services.annotations import (
    AnnotationClassInUse,
    AnnotationClassInvalid,
    AnnotationClassOrderLocked,
    AnnotationImageNotFound,
    AnnotationLimitExceeded,
    AnnotationRevisionConflict,
    AnnotationService,
)
from app.services.result_artifacts import (
    ManifestInvalidError,
    validate_dataset_manifest,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)


def manifest(job_id, run_token) -> StoredDatasetManifestV1:
    digest = "a" * 64
    return StoredDatasetManifestV1.model_validate(
        {
            "schema_version": 1,
            "dataset_type": "image",
            "job_id": job_id,
            "run_token": run_token,
            "created_at": datetime.now(UTC),
            "summary": {
                "uploaded_files": 1,
                "accepted_files": 1,
                "normal": 1,
                "challenging": 0,
                "unusable": 0,
                "rejected": 0,
                "duplicates": 0,
                "ignored_metadata_entries": 0,
                "recommended_count": 1,
                "recommended_normal": 1,
                "recommended_challenging": 0,
                "target_challenging_ratio": 0.2,
                "actual_challenging_ratio": 0.0,
                "ratio_note": None,
            },
            "recommended_indices": [0],
            "images": [
                {
                    "index": 0,
                    "filename": "image_000001.jpg",
                    "content_type": "image/jpeg",
                    "size_bytes": 100,
                    "sha256": digest,
                    "width": 800,
                    "height": 600,
                    "quality_category": "normal",
                    "sharpness": 100.0,
                    "brightness": 120.0,
                    "underexposed_ratio": 0.0,
                    "overexposed_ratio": 0.0,
                    "resolution_usable": True,
                    "quality_score": 0.9,
                    "duplicate": False,
                    "object_key": (
                        f"jobs/{job_id}/results/{run_token}/images/image_000001.jpg"
                    ),
                    "yolo_object_key": (
                        f"jobs/{job_id}/results/{run_token}/yolo/image_000001.jpg"
                    ),
                    "yolo_size_bytes": 90,
                    "yolo_sha256": "b" * 64,
                    "output_width": 640,
                    "output_height": 640,
                    "resize_scale": 0.8,
                    "padding": {"top": 80, "right": 0, "bottom": 80, "left": 0},
                }
            ],
            "exports": {
                "accepted": {"object_key": "safe", "size_bytes": 1, "sha256": digest},
                "yolo": {"object_key": "safe", "size_bytes": 1, "sha256": digest},
            },
        }
    )


class FakeResults:
    def __init__(self, job, document) -> None:
        self.job = job
        self.document = document

    async def annotation_source(self, job_id):
        assert job_id == self.job.id
        return self.job, self.document


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("images"),
        lambda value: value["images"][0].update(index=-1),
        lambda value: value["images"][0].update(index=True),
        lambda value: value["images"][0].update(index="0"),
        lambda value: value["images"].append(dict(value["images"][0])),
        lambda value: value["images"][0].update(
            object_key=value["images"][0]["object_key"] + ".tampered"
        ),
        lambda value: value["images"][0].pop("yolo_object_key"),
    ],
)
def test_annotation_manifest_rejects_missing_duplicate_and_inconsistent_images(
    mutate,
) -> None:
    job_id, run_token = uuid4(), uuid4()
    value = json.loads(manifest(job_id, run_token).model_dump_json())
    mutate(value)
    with pytest.raises(ManifestInvalidError):
        validate_dataset_manifest(json.dumps(value).encode(), job_id, run_token)


def job(job_id, run_token) -> ProcessingJob:
    now = datetime.now(UTC)
    return ProcessingJob(
        id=job_id,
        status=JobStatus.SUCCEEDED,
        source_type=SourceType.IMAGE_DATASET,
        source_display="1 image",
        source_secret=None,
        source_reference={"schema_version": 1, "items": []},
        processing_config={},
        created_at=now,
        started_at=now,
        completed_at=now,
        failure_code=None,
        failure_message=None,
        attempt_count=1,
        idempotency_scope="annotation-test",
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


async def exercise_postgres_concurrency() -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    job_id, run_token = uuid4(), uuid4()
    item = job(job_id, run_token)
    document = manifest(job_id, run_token)
    results = FakeResults(item, document)
    try:
        async with sessions() as session:
            session.add(item)
            await session.commit()
        async with sessions() as left, sessions() as right:
            created = await asyncio.gather(
                AnnotationService(left, results).get_or_create(job_id),
                AnnotationService(right, results).get_or_create(job_id),
            )
        assert sum(was_created for _, was_created in created) == 1
        assert created[0][0].id == created[1][0].id
        project_id = created[0][0].id
        async with sessions() as session:
            service = AnnotationService(session, results)
            project = await service.get(job_id)
            class_result = await service.create_class(
                project,
                CreateAnnotationClassRequest(
                    expected_revision=0, name="  U\u0308lker ", color="#abcdef"
                ),
            )
            assert class_result.revision == 1
            assert class_result.annotation_class is not None
            assert class_result.annotation_class.name == "Ülker"
            assert class_result.annotation_class.yolo_index == 0
            class_id = class_result.annotation_class.id
        async with sessions() as left, sessions() as right:
            left_service = AnnotationService(left, results)
            right_service = AnnotationService(right, results)
            left_project = await left_service.get(job_id)
            right_project = await right_service.get(job_id)
            left_box, right_box = uuid4(), uuid4()

            def request(box_id):
                return PutImageAnnotationsRequest.model_validate(
                    {
                        "expected_revision": 1,
                        "completed": True,
                        "boxes": [
                            {
                                "id": str(box_id),
                                "class_id": str(class_id),
                                "x_center": 0.5,
                                "y_center": 0.5,
                                "width": 0.2,
                                "height": 0.2,
                            }
                        ],
                    }
                )

            outcomes = await asyncio.gather(
                left_service.put_image(left_project, 0, request(left_box)),
                right_service.put_image(right_project, 0, request(right_box)),
                return_exceptions=True,
            )
        assert (
            sum(isinstance(value, AnnotationRevisionConflict) for value in outcomes)
            == 1
        )
        winner = next(value for value in outcomes if not isinstance(value, Exception))
        assert winner.project_revision == 2
        async with sessions() as session:
            persisted = await session.get(AnnotationProject, project_id)
            boxes = list(
                (
                    await session.scalars(
                        sa.select(AnnotationBox).where(
                            AnnotationBox.project_id == project_id
                        )
                    )
                ).all()
            )
            assert persisted is not None and persisted.revision == 2
            assert len(boxes) == 1
            image = await session.get(AnnotationImage, (project_id, 0))
            assert image is not None and image.completed is True
            assert boxes[0].id == winner.boxes[0].id
            original_box_id = boxes[0].id

        async with sessions() as session:
            service = AnnotationService(session, results)
            project = await service.get(job_id)
            with pytest.raises(AnnotationClassInUse):
                await service.delete_class(project, class_id, 2)
            assert project.revision == 2

            second = await service.create_class(
                project,
                CreateAnnotationClassRequest(
                    expected_revision=2, name="person", color="#112233"
                ),
            )
            third = await service.create_class(
                project,
                CreateAnnotationClassRequest(
                    expected_revision=3, name="bicycle", color="#445566"
                ),
            )
            assert second.annotation_class is not None
            assert third.annotation_class is not None
            with pytest.raises(AnnotationClassOrderLocked):
                await service.delete_class(project, second.annotation_class.id, 4)
            renamed = await service.update_class(
                project,
                third.annotation_class.id,
                CreateAnnotationClassRequest(
                    expected_revision=4, name="  bisiklet  ", color="#778899"
                ),
            )
            assert renamed.annotation_class is not None
            assert (
                renamed.annotation_class.yolo_index == third.annotation_class.yolo_index
            )
            assert renamed.annotation_class.name == "bisiklet"
            with pytest.raises(AnnotationClassInvalid):
                await service.update_class(
                    project,
                    third.annotation_class.id,
                    CreateAnnotationClassRequest(
                        expected_revision=5, name="Ülker", color="#778899"
                    ),
                )
            persisted = await session.get(AnnotationProject, project_id)
            assert persisted is not None and persisted.revision == 5
            with pytest.raises(AnnotationLimitExceeded):
                await AnnotationService(session, results, max_classes=3).create_class(
                    persisted,
                    CreateAnnotationClassRequest(
                        expected_revision=5, name="truck", color="#AABBCC"
                    ),
                )
            persisted = await session.get(AnnotationProject, project_id)
            assert persisted is not None and persisted.revision == 5

        collision_id = uuid4()
        other_project_id = uuid4()
        other_class_id = uuid4()
        now = datetime.now(UTC)
        async with sessions() as session:
            session.add(
                AnnotationProject(
                    id=other_project_id,
                    job_id=job_id,
                    result_run_token=uuid4(),
                    revision=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    AnnotationClass(
                        id=other_class_id,
                        project_id=other_project_id,
                        yolo_index=0,
                        name="foreign",
                        normalized_name="foreign",
                        color="#ABCDEF",
                        created_at=now,
                        updated_at=now,
                    ),
                    AnnotationImage(
                        project_id=other_project_id,
                        image_index=0,
                        image_filename="foreign.jpg",
                        image_sha256="c" * 64,
                        yolo_sha256="d" * 64,
                        completed=False,
                        updated_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                AnnotationBox(
                    id=collision_id,
                    project_id=other_project_id,
                    image_index=0,
                    class_id=other_class_id,
                    x_center=Decimal("0.5"),
                    y_center=Decimal("0.5"),
                    width=Decimal("0.2"),
                    height=Decimal("0.2"),
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

        collision_request = PutImageAnnotationsRequest(
            expected_revision=5,
            completed=False,
            boxes=[
                {
                    "id": collision_id,
                    "class_id": class_id,
                    "x_center": Decimal("0.5"),
                    "y_center": Decimal("0.5"),
                    "width": Decimal("0.2"),
                    "height": Decimal("0.2"),
                }
            ],
        )
        async with sessions() as session:
            service = AnnotationService(session, results)
            project = await service.get(job_id)
            with pytest.raises(AnnotationImageNotFound):
                await service.put_image(project, 999, collision_request)
            with pytest.raises(AnnotationClassInvalid):
                await service.update_class(
                    project,
                    other_class_id,
                    CreateAnnotationClassRequest(
                        expected_revision=5, name="foreign", color="#ABCDEF"
                    ),
                )
            with pytest.raises(AnnotationLimitExceeded):
                await AnnotationService(
                    session, results, max_boxes_per_image=0
                ).put_image(project, 0, collision_request)
            with pytest.raises(AnnotationLimitExceeded):
                await AnnotationService(
                    session, results, max_boxes_per_project=0
                ).put_image(project, 0, collision_request)
            with pytest.raises(AnnotationClassInvalid):
                await service.put_image(project, 0, collision_request)

            persisted = await session.get(AnnotationProject, project_id)
            preserved = list(
                (
                    await session.scalars(
                        sa.select(AnnotationBox).where(
                            AnnotationBox.project_id == project_id
                        )
                    )
                ).all()
            )
            assert persisted is not None and persisted.revision == 5
            assert [box.id for box in preserved] == [original_box_id]
            assert await session.scalar(sa.select(sa.literal(1))) == 1

        new_run_token = uuid4()
        results.document = manifest(job_id, new_run_token)
        async with sessions() as session:
            new_project, created = await AnnotationService(
                session, results
            ).get_or_create(job_id)
            assert created and new_project.id != project_id
            projects = list(
                (
                    await session.scalars(
                        sa.select(AnnotationProject).where(
                            AnnotationProject.job_id == job_id
                        )
                    )
                ).all()
            )
            assert {value.id for value in projects} == {
                project_id,
                other_project_id,
                new_project.id,
            }
    finally:
        async with sessions() as session:
            await session.execute(
                sa.delete(ProcessingJob).where(ProcessingJob.id == job_id)
            )
            await session.commit()
        await engine.dispose()


def test_real_postgres_create_and_revision_conflict() -> None:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(exercise_postgres_concurrency())
    else:
        asyncio.run(exercise_postgres_concurrency())
