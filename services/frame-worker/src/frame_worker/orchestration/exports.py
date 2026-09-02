import hashlib
import json
import re
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
    update,
)
from sqlalchemy import Uuid as SQLUuid

from frame_worker.orchestration.config import WorkerSettings

metadata = MetaData()
exports = Table(
    "frame_exports",
    metadata,
    Column("id", SQLUuid, primary_key=True),
    Column("job_id", SQLUuid),
    Column("result_run_token", SQLUuid),
    Column("status", String),
    Column("mode", String),
    Column("frame_indices", JSON),
    Column("artifact_reference", Text),
    Column("artifact_size_bytes", BigInteger),
    Column("artifact_sha256", String),
    Column("completed_at", DateTime(timezone=True)),
    Column("failure_code", String),
    Column("lease_token", SQLUuid),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("attempt_generation", Integer),
    Column("working_object_key", Text),
)
jobs = Table(
    "processing_jobs",
    metadata,
    Column("id", SQLUuid, primary_key=True),
    Column("status", String),
    Column("run_token", SQLUuid),
    Column("result_reference", Text),
)
SAFE_SOURCE_NAME = re.compile(r"^frame_\d{6}_\d+ms_\d+x\d+\.jpg$")
TEMP_PREFIX = "frame-export-"


class ExportError(RuntimeError):
    """Base class whose message is safe for task logs."""


class PermanentExportError(ExportError, ValueError):
    pass


class TransientExportError(ExportError):
    pass


class ExportLeaseBusy(TransientExportError):
    pass


class ExportLeaseLost(TransientExportError):
    pass


@dataclass(frozen=True)
class Claim:
    export: dict
    token: UUID
    generation: int
    object_key: str
    stale_object_key: str | None


def _entry_name(position: int, timestamp_ms: int, content_type: str) -> str:
    extension = {"image/jpeg": "jpg", "image/png": "png"}.get(content_type)
    if extension is None or timestamp_ms < 0:
        raise PermanentExportError("Unsupported frame metadata")
    hours, remainder = divmod(timestamp_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    prefix = f"frame_{position + 1:04d}_{hours:02d}-{minutes:02d}-{seconds:02d}"
    return f"{prefix}.{millis:03d}.{extension}"


def _temp_root(settings: WorkerSettings) -> Path:
    base = settings.processing_temp_root or Path(tempfile.gettempdir())
    return (base / "frame-intelligence-exports").resolve()


def cleanup_stale_temp(root: Path, max_age_seconds: int) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time()
    for candidate in root.iterdir():
        try:
            resolved = candidate.resolve(strict=True)
            if (
                candidate.name.startswith(TEMP_PREFIX)
                and resolved.parent == root
                and resolved.is_dir()
                and now - resolved.stat().st_mtime > max_age_seconds
            ):
                shutil.rmtree(resolved)
        except (FileNotFoundError, OSError):
            continue


def _claim(engine, export_id: UUID, settings: WorkerSettings) -> Claim | None:
    now = datetime.now(UTC)
    token = uuid4()
    with engine.begin() as connection:
        row = (
            connection.execute(
                select(exports).where(exports.c.id == export_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["status"] in {"READY", "FAILED"}:
            return None
        lease_expires_at = row["lease_expires_at"]
        if lease_expires_at is not None and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
        if lease_expires_at is not None and lease_expires_at > now:
            raise ExportLeaseBusy("Export lease is active")
        generation = (row["attempt_generation"] or 0) + 1
        key = (
            f"jobs/{row['job_id']}/exports/{export_id}/attempt-{generation}-{token}.zip"
        )
        connection.execute(
            update(exports)
            .where(exports.c.id == export_id)
            .values(
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=settings.export_lease_seconds),
                attempt_generation=generation,
                working_object_key=key,
            )
        )
        return Claim(dict(row), token, generation, key, row["working_object_key"])


def _owned(engine, claim: Claim, **values) -> bool:
    with engine.begin() as connection:
        result = connection.execute(
            update(exports)
            .where(
                exports.c.id == claim.export["id"],
                exports.c.status == "PREPARING",
                exports.c.lease_token == claim.token,
                exports.c.attempt_generation == claim.generation,
            )
            .values(**values)
        )
        return result.rowcount == 1


def _heartbeat(engine, claim: Claim, settings: WorkerSettings) -> None:
    if not _owned(
        engine,
        claim,
        lease_expires_at=datetime.now(UTC)
        + timedelta(seconds=settings.export_lease_seconds),
    ):
        raise ExportLeaseLost("Export lease was lost")


def fail_unleased_export(export_id: UUID, settings: WorkerSettings) -> None:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                update(exports)
                .where(
                    exports.c.id == export_id,
                    exports.c.status == "PREPARING",
                    (exports.c.lease_token.is_(None))
                    | (exports.c.lease_expires_at <= now),
                )
                .values(
                    status="FAILED",
                    completed_at=now,
                    failure_code="EXPORT_RETRY_EXHAUSTED",
                    lease_token=None,
                    lease_expires_at=None,
                    working_object_key=None,
                )
            )
    finally:
        engine.dispose()


def _delete_attempt(client, bucket: str, key: str | None, job_id: UUID) -> None:
    prefix = f"jobs/{job_id}/exports/"
    if key and key.startswith(prefix) and ".." not in key.split("/"):
        try:
            client.delete_object(Bucket=bucket, Key=key)
        except Exception:
            pass


def _client(settings: WorkerSettings):
    return boto3.client(
        "s3",
        endpoint_url=settings.object_storage_endpoint,
        aws_access_key_id=settings.object_storage_access_key,
        aws_secret_access_key=settings.object_storage_secret_key,
        region_name=settings.object_storage_region,
        config=Config(
            s3={"addressing_style": settings.object_storage_addressing_style},
            connect_timeout=settings.export_s3_connect_timeout_seconds,
            read_timeout=settings.export_s3_read_timeout_seconds,
            retries={"total_max_attempts": 2, "mode": "standard"},
        ),
    )


def create_export(export_id: UUID, settings: WorkerSettings) -> bool:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    client = _client(settings)
    claim: Claim | None = None
    uploaded = False
    root = _temp_root(settings)
    try:
        cleanup_stale_temp(root, settings.export_stale_temp_seconds)
        claim = _claim(engine, export_id, settings)
        if claim is None:
            return False
        _delete_attempt(
            client,
            settings.object_storage_bucket,
            claim.stale_object_key,
            claim.export["job_id"],
        )
        with engine.connect() as connection:
            job = (
                connection.execute(
                    select(jobs).where(jobs.c.id == claim.export["job_id"])
                )
                .mappings()
                .one_or_none()
            )
        if (
            job is None
            or job["status"] != "SUCCEEDED"
            or job["run_token"] is not None
            or not job["result_reference"]
        ):
            raise PermanentExportError("Result unavailable")
        prefix = f"s3://{settings.object_storage_bucket}/"
        if not job["result_reference"].startswith(prefix):
            raise PermanentExportError("Invalid result reference")
        manifest_key = job["result_reference"][len(prefix) :]
        run_token = claim.export["result_run_token"]
        expected_prefix = f"jobs/{claim.export['job_id']}/results/{run_token}/"
        if (
            not manifest_key.startswith(expected_prefix)
            or manifest_key != expected_prefix + "manifest.json"
            or ".." in manifest_key.split("/")
            or "\\" in manifest_key
        ):
            raise PermanentExportError("Invalid result reference")
        manifest_stream = client.get_object(
            Bucket=settings.object_storage_bucket, Key=manifest_key
        )["Body"]
        try:
            manifest_body = manifest_stream.read(1_048_577)
        finally:
            manifest_stream.close()
        if len(manifest_body) > 1_048_576:
            raise PermanentExportError("Manifest too large")
        try:
            manifest = json.loads(manifest_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PermanentExportError("Invalid manifest") from error
        frames = manifest.get("frames")
        indices = claim.export["frame_indices"]
        if (
            manifest.get("job_id") != str(claim.export["job_id"])
            or manifest.get("run_token") != str(run_token)
            or not isinstance(frames, list)
            or not isinstance(indices, list)
            or not indices
            or indices != sorted(set(indices))
            or claim.export["mode"] not in {"all", "selected"}
            or (claim.export["mode"] == "all" and indices != list(range(len(frames))))
        ):
            raise PermanentExportError("Invalid selection")
        if len(indices) > settings.export_max_frames:
            raise PermanentExportError("Frame limit exceeded")
        selected = []
        declared_total = 0
        for index in indices:
            if type(index) is not int or index < 0 or index >= len(frames):
                raise PermanentExportError("Invalid selection")
            frame = frames[index]
            if frame.get("index") != index or not SAFE_SOURCE_NAME.fullmatch(
                frame.get("filename", "")
            ):
                raise PermanentExportError("Invalid frame")
            if (
                frame.get("object_key")
                != expected_prefix + "frames/" + frame["filename"]
                or frame.get("content_type") != "image/jpeg"
                or type(frame.get("size_bytes")) is not int
                or frame["size_bytes"] <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", frame.get("sha256", ""))
                or type(frame.get("timestamp_ms")) is not int
            ):
                raise PermanentExportError("Invalid frame metadata")
            declared_total += frame["size_bytes"]
            if declared_total > settings.export_max_total_source_bytes:
                raise PermanentExportError("Source byte limit exceeded")
            selected.append(frame)
        if shutil.disk_usage(root).free < settings.export_min_temp_free_bytes:
            raise PermanentExportError("Insufficient temporary disk")
        last_heartbeat = time.monotonic()
        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, dir=root) as directory:
            archive = Path(directory) / "frames.zip"
            public_frames = []
            actual_total = 0
            with zipfile.ZipFile(
                archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True
            ) as output:
                for position, frame in enumerate(selected):
                    if (
                        time.monotonic() - last_heartbeat
                        >= settings.export_heartbeat_interval_seconds
                    ):
                        _heartbeat(engine, claim, settings)
                        last_heartbeat = time.monotonic()
                    name = _entry_name(
                        position, frame["timestamp_ms"], frame["content_type"]
                    )
                    source = client.get_object(
                        Bucket=settings.object_storage_bucket,
                        Key=frame["object_key"],
                    )["Body"]
                    digest = hashlib.sha256()
                    size = 0
                    try:
                        with output.open(name, "w", force_zip64=True) as target:
                            while chunk := source.read(1024 * 1024):
                                size += len(chunk)
                                actual_total += len(chunk)
                                if (
                                    size > frame["size_bytes"]
                                    or actual_total
                                    > settings.export_max_total_source_bytes
                                ):
                                    raise PermanentExportError(
                                        "Source byte limit exceeded"
                                    )
                                digest.update(chunk)
                                target.write(chunk)
                                if (
                                    time.monotonic() - last_heartbeat
                                    >= settings.export_heartbeat_interval_seconds
                                ):
                                    _heartbeat(engine, claim, settings)
                                    last_heartbeat = time.monotonic()
                                if (
                                    archive.stat().st_size
                                    > settings.export_max_temp_bytes
                                ):
                                    raise PermanentExportError(
                                        "Temp byte limit exceeded"
                                    )
                    finally:
                        source.close()
                    if (
                        size != frame["size_bytes"]
                        or digest.hexdigest() != frame["sha256"]
                    ):
                        raise PermanentExportError("Frame integrity failure")
                    public_frames.append(
                        {
                            "index": frame["index"],
                            "filename": name,
                            "timestamp_ms": frame["timestamp_ms"],
                            "size_bytes": size,
                            "content_type": frame["content_type"],
                            "sha256": frame["sha256"],
                        }
                    )
                output.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            "schema_version": 1,
                            "job_id": str(claim.export["job_id"]),
                            "export_id": str(export_id),
                            "frames": public_frames,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ).encode(),
                )
            size = archive.stat().st_size
            if (
                size > settings.export_max_zip_bytes
                or size > settings.export_max_temp_bytes
            ):
                raise PermanentExportError("ZIP byte limit exceeded")
            digest = hashlib.sha256()
            with archive.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
            _heartbeat(engine, claim, settings)
            heartbeat_stop = threading.Event()
            heartbeat_lost = threading.Event()

            def maintain_upload_lease() -> None:
                while not heartbeat_stop.wait(
                    settings.export_heartbeat_interval_seconds
                ):
                    try:
                        _heartbeat(engine, claim, settings)
                    except Exception:
                        heartbeat_lost.set()
                        return

            heartbeat_thread = threading.Thread(
                target=maintain_upload_lease,
                name="frame-export-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
            try:
                uploaded = True
                with archive.open("rb") as source:
                    client.upload_fileobj(
                        source,
                        settings.object_storage_bucket,
                        claim.object_key,
                        ExtraArgs={
                            "ContentType": "application/zip",
                            "Metadata": {"sha256": sha256},
                        },
                    )
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(
                    timeout=settings.export_heartbeat_interval_seconds + 1
                )
            if heartbeat_lost.is_set():
                raise ExportLeaseLost("Export lease was lost")
            head = client.head_object(
                Bucket=settings.object_storage_bucket, Key=claim.object_key
            )
            if (
                head.get("ContentLength") != size
                or head.get("ContentType") != "application/zip"
                or head.get("Metadata", {}).get("sha256") != sha256
            ):
                raise TransientExportError("Uploaded artifact verification failed")
        if not _owned(
            engine,
            claim,
            status="READY",
            completed_at=datetime.now(UTC),
            artifact_reference=(
                f"s3://{settings.object_storage_bucket}/{claim.object_key}"
            ),
            artifact_size_bytes=size,
            artifact_sha256=sha256,
            failure_code=None,
            lease_token=None,
            lease_expires_at=None,
            working_object_key=None,
        ):
            raise ExportLeaseLost("Export lease was lost")
        uploaded = False
        return True
    except PermanentExportError:
        if claim is not None:
            _owned(
                engine,
                claim,
                status="FAILED",
                completed_at=datetime.now(UTC),
                failure_code="EXPORT_INVALID",
                lease_token=None,
                lease_expires_at=None,
                working_object_key=None,
            )
        raise
    except (
        BotoCoreError,
        ClientError,
        OSError,
        SoftTimeLimitExceeded,
        TransientExportError,
    ) as error:
        if claim is not None:
            _owned(
                engine,
                claim,
                lease_token=None,
                lease_expires_at=None,
                working_object_key=None,
            )
        if isinstance(error, TransientExportError):
            raise
        raise TransientExportError("Temporary export dependency failure") from error
    except Exception as error:
        if claim is not None:
            _owned(
                engine,
                claim,
                lease_token=None,
                lease_expires_at=None,
                working_object_key=None,
            )
        raise TransientExportError("Unexpected export failure") from error
    finally:
        if uploaded and claim is not None:
            _delete_attempt(
                client,
                settings.object_storage_bucket,
                claim.object_key,
                claim.export["job_id"],
            )
        engine.dispose()
