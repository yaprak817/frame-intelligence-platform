import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import create_engine, insert, select, text, update

from frame_worker.orchestration import exports as exports_module
from frame_worker.orchestration.config import WorkerSettings
from frame_worker.orchestration.exports import (
    Claim,
    ExportLeaseBusy,
    ExportLeaseLost,
    PermanentExportError,
    TransientExportError,
    _claim,
    _entry_name,
    _heartbeat,
    _owned,
    cleanup_stale_temp,
    create_export,
    exports,
    jobs,
    metadata,
)


def test_zip_entry_name_is_safe_ascii_and_chronological() -> None:
    name = _entry_name(0, 5_200, "image/jpeg")
    assert name == "frame_0001_00-00-05.200.jpg"
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", name)
    assert ".." not in name and "/" not in name and "\\" not in name


def test_zip_entry_name_rejects_unknown_media_type() -> None:
    with pytest.raises(ValueError):
        _entry_name(0, 0, "application/octet-stream")


def _settings(tmp_path, database_url: str) -> WorkerSettings:
    return WorkerSettings(
        database_url=database_url,
        celery_broker_url="redis://localhost/0",
        job_source_encryption_key="VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ=",
        object_storage_endpoint="http://localhost:9000",
        object_storage_access_key="test",
        object_storage_secret_key="test",
        object_storage_bucket="test",
        object_storage_region="us-east-1",
        object_storage_addressing_style="path",
        max_download_bytes=1024,
        lease_seconds=60,
        heartbeat_interval_seconds=10,
        visibility_timeout_seconds=120,
        worker_concurrency=1,
        processing_temp_root=tmp_path,
        export_lease_seconds=60,
    )


def test_claim_is_atomic_and_fenced_and_expired_lease_is_recoverable(tmp_path) -> None:
    database = tmp_path / "exports.sqlite"
    engine = create_engine(f"sqlite:///{database}")
    metadata.create_all(engine, tables=[exports])
    export_id, job_id, run_token = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(exports).values(
                id=export_id,
                job_id=job_id,
                result_run_token=run_token,
                status="PREPARING",
                mode="all",
                frame_indices=[0],
                attempt_generation=0,
            )
        )
    settings = _settings(tmp_path, f"sqlite:///{database}")
    first = _claim(engine, export_id, settings)
    assert first is not None and first.generation == 1
    with pytest.raises(ExportLeaseBusy):
        _claim(engine, export_id, settings)
    with engine.begin() as connection:
        connection.execute(
            update(exports)
            .where(exports.c.id == export_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    second = _claim(engine, export_id, settings)
    assert second is not None and second.generation == 2 and second.token != first.token
    assert not _owned(engine, first, status="READY")
    assert _owned(engine, second, status="READY")


def test_stale_temp_cleanup_is_allowlisted(tmp_path) -> None:
    root = tmp_path / "frame-intelligence-exports"
    stale = root / "frame-export-old"
    unrelated = root / "keep-me"
    stale.mkdir(parents=True)
    unrelated.mkdir()
    old = (datetime.now(UTC) - timedelta(hours=3)).timestamp()
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))
    cleanup_stale_temp(root.resolve(), 60)
    assert not stale.exists() and unrelated.exists()


class _Body:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class _Storage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.metadata: dict[str, dict[str, str]] = {}
        self.deleted: list[str] = []
        self.uploads = 0
        self.upload_error: Exception | None = None
        self.bad_head = False

    def get_object(self, *, Key: str, **_kwargs):
        payload = self.objects[Key]
        return {"Body": _Body(payload)}

    def upload_fileobj(self, source, _bucket: str, key: str, ExtraArgs):
        self.uploads += 1
        payload = source.read()
        self.objects[key] = payload
        self.metadata[key] = dict(ExtraArgs["Metadata"])
        if self.upload_error:
            raise self.upload_error

    def head_object(self, *, Key: str, **_kwargs):
        payload = self.objects[Key]
        return {
            "ContentLength": len(payload) + (1 if self.bad_head else 0),
            "ContentType": "application/zip",
            "Metadata": self.metadata[Key],
        }

    def delete_object(self, *, Key: str, **_kwargs):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


def _export_fixture(tmp_path, *, frame_payloads=(b"abcd", b"efgh")):
    database = tmp_path / f"export-{uuid4()}.sqlite"
    engine = create_engine(f"sqlite:///{database}")
    metadata.create_all(engine, tables=[jobs, exports])
    export_id, job_id, run_token = uuid4(), uuid4(), uuid4()
    prefix = f"jobs/{job_id}/results/{run_token}"
    frames = []
    objects = {}
    for index, payload in enumerate(frame_payloads):
        filename = f"frame_{index:06d}_{index * 1000}ms_640x480.jpg"
        key = f"{prefix}/frames/{filename}"
        objects[key] = payload
        frames.append(
            {
                "index": index,
                "filename": filename,
                "object_key": key,
                "content_type": "image/jpeg",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "timestamp_ms": index * 1000,
            }
        )
    manifest_key = f"{prefix}/manifest.json"
    objects[manifest_key] = json.dumps(
        {"job_id": str(job_id), "run_token": str(run_token), "frames": frames}
    ).encode()
    with engine.begin() as connection:
        connection.execute(
            insert(jobs).values(
                id=job_id,
                status="SUCCEEDED",
                run_token=None,
                result_reference=f"s3://test/{manifest_key}",
            )
        )
        connection.execute(
            insert(exports).values(
                id=export_id,
                job_id=job_id,
                result_run_token=run_token,
                status="PREPARING",
                mode="all",
                frame_indices=list(range(len(frames))),
                attempt_generation=0,
            )
        )
    engine.dispose()
    return (
        export_id,
        job_id,
        frames,
        _Storage(objects),
        _settings(tmp_path, f"sqlite:///{database}"),
    )


def _run_export(monkeypatch, export_id, storage, settings):
    monkeypatch.setattr(exports_module, "_client", lambda _settings: storage)
    return create_export(export_id, settings)


def test_success_is_idempotent_and_db_matches_immutable_artifact(
    monkeypatch, tmp_path
) -> None:
    export_id, _job_id, _frames, storage, settings = _export_fixture(tmp_path)
    assert _run_export(monkeypatch, export_id, storage, settings)
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        row = (
            connection.execute(select(exports).where(exports.c.id == export_id))
            .mappings()
            .one()
        )
    engine.dispose()
    key = row["artifact_reference"].removeprefix("s3://test/")
    artifact = storage.objects[key]
    assert row["status"] == "READY"
    assert row["artifact_size_bytes"] == len(artifact)
    assert row["artifact_sha256"] == hashlib.sha256(artifact).hexdigest()
    assert not _run_export(monkeypatch, export_id, storage, settings)
    assert storage.uploads == 1
    assert list((tmp_path / "frame-intelligence-exports").iterdir()) == []


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("export_max_frames", 1, "Frame limit exceeded"),
        ("export_max_total_source_bytes", 7, "Source byte limit exceeded"),
        ("export_max_temp_bytes", 1, "Temp byte limit exceeded"),
        ("export_max_zip_bytes", 1, "ZIP byte limit exceeded"),
    ],
)
def test_worker_enforces_export_resource_limits(
    monkeypatch, tmp_path, setting, value, message
) -> None:
    export_id, _job_id, _frames, storage, settings = _export_fixture(tmp_path)
    settings = replace(settings, **{setting: value}, export_min_temp_free_bytes=1)
    with pytest.raises(PermanentExportError, match=message):
        _run_export(monkeypatch, export_id, storage, settings)
    assert list((tmp_path / "frame-intelligence-exports").iterdir()) == []


def test_streamed_actual_bytes_cannot_exceed_manifest(monkeypatch, tmp_path) -> None:
    export_id, _job_id, frames, storage, settings = _export_fixture(tmp_path)
    frames[0]["size_bytes"] -= 1
    manifest_key = next(key for key in storage.objects if key.endswith("manifest.json"))
    manifest = json.loads(storage.objects[manifest_key])
    manifest["frames"] = frames
    storage.objects[manifest_key] = json.dumps(manifest).encode()
    with pytest.raises(PermanentExportError, match="Source byte limit exceeded"):
        _run_export(monkeypatch, export_id, storage, settings)


def test_minimum_free_disk_is_enforced(monkeypatch, tmp_path) -> None:
    export_id, _job_id, _frames, storage, settings = _export_fixture(tmp_path)
    monkeypatch.setattr(
        exports_module.shutil, "disk_usage", lambda _root: SimpleNamespace(free=0)
    )
    with pytest.raises(PermanentExportError, match="Insufficient temporary disk"):
        _run_export(monkeypatch, export_id, storage, settings)


@pytest.mark.parametrize(
    "failure",
    [OSError("upload failed"), SoftTimeLimitExceeded()],
)
def test_upload_and_soft_timeout_remove_partial_object_and_temp(
    monkeypatch, tmp_path, failure
) -> None:
    export_id, _job_id, _frames, storage, settings = _export_fixture(tmp_path)
    storage.upload_error = failure
    with pytest.raises(TransientExportError):
        _run_export(monkeypatch, export_id, storage, settings)
    assert storage.deleted
    assert all("/attempt-" not in key for key in storage.objects)
    assert list((tmp_path / "frame-intelligence-exports").iterdir()) == []


def test_integrity_failure_cleans_temp_without_publishing(
    monkeypatch, tmp_path
) -> None:
    export_id, _job_id, frames, storage, settings = _export_fixture(tmp_path)
    frames[0]["sha256"] = "0" * 64
    manifest_key = next(key for key in storage.objects if key.endswith("manifest.json"))
    manifest = json.loads(storage.objects[manifest_key])
    manifest["frames"] = frames
    storage.objects[manifest_key] = json.dumps(manifest).encode()
    with pytest.raises(PermanentExportError, match="Frame integrity failure"):
        _run_export(monkeypatch, export_id, storage, settings)
    assert storage.uploads == 0
    assert list((tmp_path / "frame-intelligence-exports").iterdir()) == []


def test_expired_crash_attempt_is_deleted_before_recovery(
    monkeypatch, tmp_path
) -> None:
    export_id, job_id, _frames, storage, settings = _export_fixture(tmp_path)
    stale_key = f"jobs/{job_id}/exports/{export_id}/attempt-1-stale.zip"
    storage.objects[stale_key] = b"partial"
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            update(exports)
            .where(exports.c.id == export_id)
            .values(
                attempt_generation=1,
                lease_token=uuid4(),
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                working_object_key=stale_key,
            )
        )
    engine.dispose()
    assert _run_export(monkeypatch, export_id, storage, settings)
    assert stale_key in storage.deleted
    assert stale_key not in storage.objects


def test_s3_timeout_configuration_is_bounded(monkeypatch, tmp_path) -> None:
    captured = {}
    monkeypatch.setattr(
        exports_module.boto3,
        "client",
        lambda *_args, **kwargs: captured.update(kwargs) or object(),
    )
    settings = _settings(tmp_path, "sqlite://")
    settings = replace(
        settings,
        export_s3_connect_timeout_seconds=3,
        export_s3_read_timeout_seconds=7,
    )
    exports_module._client(settings)
    config = captured["config"]
    assert config.connect_timeout == 3
    assert config.read_timeout == 7
    assert config.retries["total_max_attempts"] == 2


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_postgresql_concurrent_claim_and_fencing() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(database_url)
    export_id, job_id, run_token = uuid4(), uuid4(), uuid4()
    settings = _settings(None, database_url)
    settings = replace(settings, export_lease_seconds=0.2)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO processing_jobs (
                        id, status, source_type, source_display, source_secret,
                        processing_config, created_at, attempt_count,
                        idempotency_scope, idempotency_key, request_fingerprint,
                        version
                    ) VALUES (
                        :id, 'SUCCEEDED', 'URL', 'https://example.invalid/video',
                        'protected', CAST(:config AS JSON), :created_at, 0,
                        'export-test', :key, :fingerprint, 1
                    )
                    """
                ),
                {
                    "id": job_id,
                    "config": '{"candidate_fps":5.0,"selection_window_seconds":1.0}',
                    "created_at": datetime.now(UTC),
                    "key": str(uuid4()),
                    "fingerprint": "f" * 64,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO frame_exports (
                        id, job_id, result_run_token, status, mode,
                        frame_indices, selection_hash, created_at,
                        attempt_generation
                    ) VALUES (
                        :id, :job_id, :run_token, 'PREPARING', 'all',
                        CAST(:indices AS JSON), :selection_hash, :created_at, 0
                    )
                    """
                ),
                {
                    "id": export_id,
                    "job_id": job_id,
                    "run_token": run_token,
                    "indices": "[0]",
                    "selection_hash": "s" * 64,
                    "created_at": datetime.now(UTC),
                },
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_claim, engine, export_id, settings) for _ in range(2)
            ]
            outcomes = []
            errors = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except Exception as error:
                    errors.append(error)
        assert len(outcomes) == 1 and outcomes[0] is not None
        assert len(errors) == 1 and isinstance(errors[0], ExportLeaseBusy)
    except Exception:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM processing_jobs WHERE id=:id"), {"id": job_id}
            )
        engine.dispose()
        raise
    first_row = None
    with engine.connect() as connection:
        first_row = (
            connection.execute(select(exports).where(exports.c.id == export_id))
            .mappings()
            .one()
        )
    old_claim = Claim(
        dict(first_row),
        first_row["lease_token"],
        first_row["attempt_generation"],
        first_row["working_object_key"],
        None,
    )
    with engine.begin() as connection:
        connection.execute(
            update(exports)
            .where(exports.c.id == export_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    new_claim = _claim(engine, export_id, settings)
    assert new_claim and new_claim.generation == old_claim.generation + 1
    with pytest.raises(ExportLeaseLost):
        _heartbeat(engine, old_claim, settings)
    assert not _owned(engine, old_claim, status="FAILED")
    assert not _owned(engine, old_claim, status="READY")
    assert _owned(engine, new_claim, status="READY")
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM processing_jobs WHERE id=:id"), {"id": job_id}
        )
    engine.dispose()
