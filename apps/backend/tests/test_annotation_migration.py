import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from app.domain.jobs import JobStatus, SourceType
from app.models.annotations import AnnotationProject
from app.models.processing_job import ProcessingJob


def test_annotation_migration_is_the_single_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["20260909_0006"]


async def inspect_schema() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: set(sa.inspect(sync).get_table_names())
            )
            project_checks = await connection.run_sync(
                lambda sync: {
                    item["name"]
                    for item in sa.inspect(sync).get_check_constraints(
                        "annotation_projects"
                    )
                }
            )
            box_checks = await connection.run_sync(
                lambda sync: {
                    item["name"]
                    for item in sa.inspect(sync).get_check_constraints(
                        "annotation_boxes"
                    )
                }
            )
        assert {
            "annotation_projects",
            "annotation_classes",
            "annotation_images",
            "annotation_boxes",
        } <= tables
        assert not any("annotation_export" in table for table in tables)
        assert "ck_annotation_projects_revision" in project_checks
        assert {
            "ck_annotation_boxes_x_center",
            "ck_annotation_boxes_y_center",
            "ck_annotation_boxes_width",
            "ck_annotation_boxes_height",
            "ck_annotation_boxes_x_bounds",
            "ck_annotation_boxes_y_bounds",
        } <= box_checks
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL") is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
def test_real_postgres_annotation_schema_constraints() -> None:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(inspect_schema())
    else:
        asyncio.run(inspect_schema())


def run_alembic(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = environment["TEST_DATABASE_URL"]
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=environment,
        check=check,
        capture_output=True,
        text=True,
    )


async def seed_annotation_project() -> tuple:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    job_id, project_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda _connection: None)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(
                ProcessingJob(
                    id=job_id,
                    status=JobStatus.SUCCEEDED,
                    source_type=SourceType.IMAGE_DATASET,
                    source_display="migration-test",
                    source_secret=None,
                    source_reference={},
                    processing_config={},
                    created_at=now,
                    started_at=now,
                    completed_at=now,
                    failure_code=None,
                    failure_message=None,
                    attempt_count=1,
                    idempotency_scope="migration-test",
                    idempotency_key=str(uuid4()),
                    request_fingerprint="f" * 64,
                    result_reference="s3://test/manifest.json",
                    result_summary={},
                    run_token=None,
                    lease_expires_at=None,
                    version=1,
                )
            )
            await session.flush()
            session.add(
                AnnotationProject(
                    id=project_id,
                    job_id=job_id,
                    result_run_token=uuid4(),
                    revision=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        return job_id, project_id
    finally:
        await engine.dispose()


async def remove_annotation_project(job_id) -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.delete(ProcessingJob).where(ProcessingJob.id == job_id)
            )
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL") is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
def test_real_postgres_migration_downgrade_refusal_and_round_trip() -> None:
    run_alembic("upgrade", "head")
    assert "20260909_0006" in run_alembic("current").stdout
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            job_id, _project_id = runner.run(seed_annotation_project())
    else:
        job_id, _project_id = asyncio.run(seed_annotation_project())

    refused = run_alembic("downgrade", "20260902_0005", check=False)
    assert refused.returncode != 0
    assert "Downgrade refused while annotation data exists" in (
        refused.stdout + refused.stderr
    )
    assert "20260909_0006" in run_alembic("current").stdout

    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(remove_annotation_project(job_id))
    else:
        asyncio.run(remove_annotation_project(job_id))
    run_alembic("downgrade", "20260902_0005")
    assert "20260902_0005" in run_alembic("current").stdout
    run_alembic("upgrade", "head")
    assert "20260909_0006" in run_alembic("current").stdout
