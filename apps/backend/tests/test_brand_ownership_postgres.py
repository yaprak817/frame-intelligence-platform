"""Real PostgreSQL ownership checks for brand datasets and workspaces."""

import asyncio
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_annotation_migration import remove_annotation_project, seed_annotation_project

from app.api.routes.brands import attach_brand_dataset
from app.models.annotations import AnnotationBox, AnnotationClass, AnnotationProject
from app.models.brands import Brand, BrandDataset
from app.schemas.brands import BrandDatasetAttach
from app.services.annotations import AnnotationBrandConflict, AnnotationService


def run_async(coroutine):
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)


async def exercise_ownership(job_id, project_id):
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    first, second = uuid4(), uuid4()
    try:
        async with sessions() as session:
            for brand_id in (first, second):
                session.add(
                    Brand(
                        id=brand_id,
                        name=str(brand_id),
                        normalized_name=str(brand_id),
                        status="DRAFT",
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.commit()

        async with sessions() as session:
            attached = await attach_brand_dataset(
                first,
                BrandDatasetAttach(name="frames", job_id=job_id),
                session,
            )
            again = await attach_brand_dataset(
                first,
                BrandDatasetAttach(name="frames again", job_id=job_id),
                session,
            )
            assert attached.id == again.id

        async with sessions() as session:
            project = await session.get(AnnotationProject, project_id)
            token = project.result_run_token

        class Results:
            async def annotation_source(self, requested_job):
                assert requested_job == job_id
                return None, SimpleNamespace(
                    run_token=token,
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

        async with sessions() as session:
            project, _ = await AnnotationService(
                session, Results()
            ).get_or_create_brand(first)
            assert project.brand_id == first
            image = await session.get(AnnotationProject, project_id)
            image.revision = 7
            class_id, box_id = uuid4(), uuid4()
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
            await session.flush()
            session.add(
                AnnotationBox(
                    id=box_id,
                    project_id=project_id,
                    image_index=0,
                    class_id=class_id,
                    x_center=Decimal("0.5"),
                    y_center=Decimal("0.5"),
                    width=Decimal("0.2"),
                    height=Decimal("0.2"),
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

        async with sessions() as session:
            with pytest.raises(HTTPException) as error:
                await attach_brand_dataset(
                    second,
                    BrandDatasetAttach(name="stolen", job_id=job_id),
                    session,
                )
            assert error.value.status_code == 409
            with pytest.raises(HTTPException) as cross_project:
                await attach_brand_dataset(
                    second,
                    BrandDatasetAttach(name="stolen", annotation_project_id=project_id),
                    session,
                )
            assert cross_project.value.status_code == 409

        async with sessions() as session:
            project = await session.get(AnnotationProject, project_id)
            assert project.brand_id == first and project.revision == 7
            assert await session.get(AnnotationBox, box_id) is not None
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(BrandDataset)
                    .where(BrandDataset.job_id == job_id)
                )
                == 1
            )
            same, created = await AnnotationService(
                session, Results()
            ).get_or_create_brand(first)
            assert same.id == project_id and not created

        # A legacy unowned project can only be claimed by one concurrent owner.
        async with sessions() as session:
            await session.execute(
                sa.delete(BrandDataset).where(BrandDataset.job_id == job_id)
            )
            project = await session.get(AnnotationProject, project_id)
            project.brand_id = None
            await session.commit()

        async def contend(brand_id):
            async with sessions() as session:
                try:
                    await attach_brand_dataset(
                        brand_id,
                        BrandDatasetAttach(name="race", job_id=job_id),
                        session,
                    )
                    await AnnotationService(session, Results()).get_or_create_brand(
                        brand_id
                    )
                    return brand_id
                except (HTTPException, AnnotationBrandConflict):
                    await session.rollback()
                    return None

        winners = await asyncio.gather(contend(first), contend(second))
        assert len([winner for winner in winners if winner is not None]) == 1
        async with sessions() as session:
            project = await session.get(AnnotationProject, project_id)
            assert project.brand_id in {first, second}
            assert project.revision == 7
            assert await session.get(AnnotationBox, box_id) is not None
    finally:
        async with sessions() as session:
            await session.execute(
                sa.delete(BrandDataset).where(
                    BrandDataset.brand_id.in_((first, second))
                )
            )
            project = await session.get(AnnotationProject, project_id)
            if project is not None:
                project.brand_id = None
            await session.execute(sa.delete(Brand).where(Brand.id.in_((first, second))))
            await session.commit()
        await engine.dispose()


@pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL") is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
def test_job_brand_ownership_is_race_safe():
    job_id, project_id, _training_id = run_async(seed_annotation_project())
    try:
        run_async(exercise_ownership(job_id, project_id))
    finally:
        assert set(
            run_async(remove_annotation_project(job_id, project_id)).values()
        ) == {0}
