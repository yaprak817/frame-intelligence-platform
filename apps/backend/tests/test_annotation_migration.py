import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from app import models  # noqa: F401
from app.db.base import Base
from app.domain.jobs import JobStatus, SourceType
from app.models.annotation_training import (
    AnnotationTrainingRun,
    AnnotationTrainingSnapshotBox,
    AnnotationTrainingSnapshotClass,
    AnnotationTrainingSnapshotImage,
)
from app.models.annotations import AnnotationProject
from app.models.processing_job import ProcessingJob

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG = BACKEND_ROOT / "alembic.ini"


def test_annotation_migration_is_the_single_head(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_CONFIG)))
    assert script.get_heads() == ["20260920_0012"]
    assert script.get_revision("20260916_0009").down_revision == "20260914_0008"
    assert script.get_revision("20260916_0010").down_revision == "20260916_0009"
    assert script.get_revision("20260916_0011").down_revision == "20260916_0010"
    assert script.get_revision("20260920_0012").down_revision == "20260916_0011"
    assert {
        "annotation_inference_runs",
        "annotation_inference_outbox",
        "brands",
        "brand_classes",
        "brand_datasets",
    } <= set(Base.metadata.tables)


async def inspect_schema(include_phase4c: bool = False) -> None:
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
            training_uniques = await connection.run_sync(
                lambda sync: {
                    item["name"]
                    for item in sa.inspect(sync).get_unique_constraints(
                        "annotation_training_runs"
                    )
                }
            )
            project_columns = await connection.run_sync(
                lambda sync: {
                    item["name"]
                    for item in sa.inspect(sync).get_columns("annotation_projects")
                }
            )
            image_columns = await connection.run_sync(
                lambda sync: {
                    item["name"]
                    for item in sa.inspect(sync).get_columns("annotation_images")
                }
            )
            box_columns = await connection.run_sync(
                lambda sync: {
                    item["name"]
                    for item in sa.inspect(sync).get_columns("annotation_boxes")
                }
            )
        assert {
            "annotation_projects",
            "annotation_classes",
            "annotation_images",
            "annotation_boxes",
            "annotation_training_runs",
            "annotation_training_snapshot_classes",
            "annotation_training_snapshot_images",
            "annotation_training_snapshot_boxes",
        } <= tables
        if include_phase4c:
            assert {
                "annotation_inference_runs",
                "annotation_inference_outbox",
                "brands",
                "brand_classes",
                "brand_datasets",
            } <= tables
            assert "brand_id" in project_columns
            assert {
                "source_job_id",
                "source_result_run_token",
                "source_image_index",
            } <= image_columns
            assert "auto_label_run_id" in box_columns
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
        assert "uq_annotation_training_runs_source_revision" in training_uniques
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL") is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
def test_real_postgres_annotation_schema_constraints(
    restore_database_revision,
) -> None:
    run_alembic("upgrade", "head")
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(inspect_schema(include_phase4c=True))
    else:
        asyncio.run(inspect_schema(include_phase4c=True))


def run_alembic(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = environment["TEST_DATABASE_URL"]
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ALEMBIC_CONFIG),
            *arguments,
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
    )


def current_revision() -> str:
    return run_alembic("current").stdout.split()[0]


def run_async(operation):
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(operation)
    return asyncio.run(operation)


async def assert_restored_revision(expected_revision: str) -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
            outbox = await connection.scalar(
                sa.text("SELECT to_regclass('annotation_training_outbox')")
            )
        assert revision == expected_revision
        if expected_revision == "20260914_0008":
            assert outbox == "annotation_training_outbox"
    finally:
        await engine.dispose()


@pytest.fixture
def restore_database_revision():
    initial_revision = current_revision()
    try:
        yield initial_revision
    finally:
        if current_revision() != initial_revision:
            script = ScriptDirectory.from_config(Config(str(ALEMBIC_CONFIG)))
            ancestors = {
                item.revision
                for item in script.iterate_revisions("head", initial_revision)
            }
            direction = "downgrade" if current_revision() in ancestors else "upgrade"
            run_alembic(direction, initial_revision)
        if sys.platform == "win32":
            with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                runner.run(assert_restored_revision(initial_revision))
        else:
            asyncio.run(assert_restored_revision(initial_revision))


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
            await session.flush()
            training_id, class_id, box_id = uuid4(), uuid4(), uuid4()
            await session.execute(
                sa.text(
                    "INSERT INTO annotation_training_runs "
                    "(id, project_id, snapshot_version, source_revision, status, "
                    "selected_image_count, selected_class_count, selected_box_count, "
                    "train_image_count, validation_image_count, config, config_hash, "
                    "idempotency_key, request_fingerprint, created_at, completed_at) "
                    "VALUES (:id, :project_id, 1, 0, 'SNAPSHOT_READY', 50, 1, 1, "
                    "40, 10, CAST(:config AS json), :config_hash, :idempotency_key, "
                    ":request_fingerprint, :created_at, :completed_at)"
                ),
                {
                    "id": training_id,
                    "project_id": project_id,
                    "config": json.dumps({"max_snapshot_images": 50}),
                    "config_hash": "a" * 64,
                    "idempotency_key": "migration-training-key",
                    "request_fingerprint": "b" * 64,
                    "created_at": now,
                    "completed_at": now,
                },
            )
            await session.flush()
            session.add(
                AnnotationTrainingSnapshotClass(
                    training_id=training_id,
                    project_id=project_id,
                    class_id=class_id,
                    yolo_index=0,
                    name="vehicle",
                )
            )
            session.add(
                AnnotationTrainingSnapshotImage(
                    training_id=training_id,
                    project_id=project_id,
                    image_index=0,
                    filename="frame.jpg",
                    source_object_key="jobs/test/frame.jpg",
                    source_size_bytes=100,
                    source_content_type="image/jpeg",
                    source_sha256="c" * 64,
                    width=640,
                    height=360,
                    timestamp_ms=0,
                    split="train",
                )
            )
            await session.flush()
            session.add(
                AnnotationTrainingSnapshotBox(
                    training_id=training_id,
                    project_id=project_id,
                    box_id=box_id,
                    image_index=0,
                    yolo_index=0,
                    x_center=Decimal("0.50000000"),
                    y_center=Decimal("0.50000000"),
                    width=Decimal("0.25000000"),
                    height=Decimal("0.25000000"),
                )
            )
            await session.commit()
        return job_id, project_id, training_id
    finally:
        await engine.dispose()


async def remove_annotation_project(job_id, project_id) -> dict[str, int]:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.delete(AnnotationTrainingSnapshotBox).where(
                    AnnotationTrainingSnapshotBox.project_id == project_id
                )
            )
            await connection.execute(
                sa.delete(AnnotationTrainingSnapshotImage).where(
                    AnnotationTrainingSnapshotImage.project_id == project_id
                )
            )
            await connection.execute(
                sa.delete(AnnotationTrainingSnapshotClass).where(
                    AnnotationTrainingSnapshotClass.project_id == project_id
                )
            )
            await connection.execute(
                sa.delete(AnnotationTrainingRun).where(
                    AnnotationTrainingRun.project_id == project_id
                )
            )
            await connection.execute(
                sa.delete(AnnotationProject).where(AnnotationProject.id == project_id)
            )
            await connection.execute(
                sa.delete(ProcessingJob).where(ProcessingJob.id == job_id)
            )
            tables = {
                "runs": AnnotationTrainingRun,
                "classes": AnnotationTrainingSnapshotClass,
                "images": AnnotationTrainingSnapshotImage,
                "boxes": AnnotationTrainingSnapshotBox,
                "projects": AnnotationProject,
                "jobs": ProcessingJob,
            }
            counts = {}
            for name, model in tables.items():
                if model is ProcessingJob:
                    identifier = ProcessingJob.id == job_id
                elif model is AnnotationProject:
                    identifier = AnnotationProject.id == project_id
                else:
                    identifier = model.project_id == project_id
                counts[name] = int(
                    await connection.scalar(
                        sa.select(sa.func.count()).select_from(model).where(identifier)
                    )
                    or 0
                )
            return counts
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL") is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
def test_real_postgres_migration_downgrade_refuses_with_data(
    restore_database_revision,
) -> None:
    run_alembic("upgrade", "head")
    assert "20260920_0012" in run_alembic("current").stdout
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            job_id, project_id, training_id = runner.run(seed_annotation_project())
    else:
        job_id, project_id, training_id = asyncio.run(seed_annotation_project())

    async def assert_seed_preserved() -> None:
        engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        sa.select(sa.func.count())
                        .select_from(AnnotationTrainingRun)
                        .where(AnnotationTrainingRun.id == training_id)
                    )
                    == 1
                )
                assert (
                    await connection.scalar(
                        sa.select(sa.func.count())
                        .select_from(AnnotationProject)
                        .where(AnnotationProject.id == project_id)
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    try:
        refused = run_alembic("downgrade", "20260909_0006", check=False)
        assert refused.returncode != 0
        assert (
            "Downgrade refused while annotation inference or training data exists"
            in (refused.stdout + refused.stderr)
        )
        assert "20260920_0012" in run_alembic("current").stdout
        run_async(assert_seed_preserved())
    finally:
        counts = run_async(remove_annotation_project(job_id, project_id))
    assert set(counts.values()) == {0}


@pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL") is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
def test_real_postgres_migration_clean_round_trip(restore_database_revision) -> None:
    run_alembic("upgrade", "head")
    job_id, project_id, _training_id = run_async(seed_annotation_project())
    counts = run_async(remove_annotation_project(job_id, project_id))
    assert set(counts.values()) == {0}

    run_alembic("downgrade", "20260916_0011")
    assert "20260916_0011" in run_alembic("current").stdout
    run_alembic("upgrade", "head")
    assert "20260920_0012" in run_alembic("current").stdout
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(inspect_schema(include_phase4c=True))
    else:
        asyncio.run(inspect_schema(include_phase4c=True))


@pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL") is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
def test_0012_refuses_published_pending_outbox_and_preserves_rows(
    restore_database_revision,
) -> None:
    run_alembic("upgrade", "head")
    job_id, project_id, training_id = run_async(seed_annotation_project())
    run_id, event_id = uuid4(), uuid4()
    now = datetime.now(UTC)

    async def seed_and_check(seed: bool) -> None:
        engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
        try:
            async with engine.begin() as connection:
                if seed:
                    await connection.execute(
                        sa.text(
                            "INSERT INTO annotation_inference_runs "
                            "(id, project_id, training_id, model_version, "
                            "status, targets, target_image_count, "
                            "processed_image_count, "
                            "created_box_count, created_at) VALUES "
                            "(:id, :project, :training, 1, 'PENDING', "
                            "CAST(:targets AS json), 1, 0, 0, :now)"
                        ),
                        {
                            "id": run_id,
                            "project": project_id,
                            "training": training_id,
                            "targets": json.dumps(
                                [{"image_index": 0, "sha256": "c" * 64}]
                            ),
                            "now": now,
                        },
                    )
                    await connection.execute(
                        sa.text(
                            "INSERT INTO annotation_inference_outbox "
                            "(id, inference_id, created_at, published_at, "
                            "attempt_count, next_attempt_at) "
                            "VALUES (:id, :run, :now, :now, 1, :now)"
                        ),
                        {"id": event_id, "run": run_id, "now": now},
                    )
                else:
                    row = (
                        await connection.execute(
                            sa.text(
                                "SELECT r.targets, o.published_at "
                                "FROM annotation_inference_runs r "
                                "JOIN annotation_inference_outbox o "
                                "ON o.inference_id = r.id "
                                "WHERE r.id = :id AND o.id = :event"
                            ),
                            {"id": run_id, "event": event_id},
                        )
                    ).one()
                    assert row.targets == [{"image_index": 0, "sha256": "c" * 64}]
                    assert row.published_at is not None
        finally:
            await engine.dispose()

    try:
        run_async(seed_and_check(True))
        refused = run_alembic("downgrade", "20260916_0011", check=False)
        assert refused.returncode != 0
        assert (
            "Cannot downgrade: annotation inference outbox is not empty"
            in refused.stderr
        )
        assert current_revision() == "20260920_0012"
        run_async(seed_and_check(False))
    finally:

        async def clean() -> None:
            engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        sa.text("DELETE FROM annotation_inference_runs WHERE id = :id"),
                        {"id": run_id},
                    )
            finally:
                await engine.dispose()

        run_async(clean())
        assert set(
            run_async(remove_annotation_project(job_id, project_id)).values()
        ) == {0}


@pytest.mark.skipif(
    os.environ.get("TEST_DATABASE_URL") is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
def test_0012_backfills_only_pending_once(restore_database_revision) -> None:
    run_alembic("upgrade", "head")
    run_alembic("downgrade", "20260916_0011")
    job_id, project_id, training_id = run_async(seed_annotation_project())
    pending_id, succeeded_id = uuid4(), uuid4()

    async def rows(seed: bool) -> tuple[int, int, int]:
        engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
        try:
            async with engine.begin() as connection:
                if seed:
                    for run_id, status in (
                        (pending_id, "PENDING"),
                        (succeeded_id, "SUCCEEDED"),
                    ):
                        await connection.execute(
                            sa.text(
                                "INSERT INTO annotation_inference_runs "
                                "(id, project_id, training_id, model_version, "
                                "status, targets, target_image_count, "
                                "processed_image_count, "
                                "created_box_count, created_at) VALUES "
                                "(:id, :project, :training, 1, :status, "
                                "'[{}]', 1, 0, 0, :now)"
                            ),
                            {
                                "id": run_id,
                                "project": project_id,
                                "training": training_id,
                                "status": status,
                                "now": datetime.now(UTC),
                            },
                        )
                    return 0, 0, 0
                pending = await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM annotation_inference_outbox "
                        "WHERE inference_id = :id AND published_at IS NULL"
                    ),
                    {"id": pending_id},
                )
                succeeded = await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM annotation_inference_outbox "
                        "WHERE inference_id = :id"
                    ),
                    {"id": succeeded_id},
                )
                duplicates = await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM annotation_inference_outbox "
                        "WHERE inference_id = :id"
                    ),
                    {"id": pending_id},
                )
                return pending, succeeded, duplicates
        finally:
            await engine.dispose()

    try:
        run_async(rows(True))
        run_alembic("upgrade", "head")
        assert run_async(rows(False)) == (1, 0, 1)
    finally:

        async def clean() -> None:
            engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        sa.text(
                            "DELETE FROM annotation_inference_runs WHERE id IN (:a, :b)"
                        ),
                        {"a": pending_id, "b": succeeded_id},
                    )
            finally:
                await engine.dispose()

        run_async(clean())
        assert set(
            run_async(remove_annotation_project(job_id, project_id)).values()
        ) == {0}
