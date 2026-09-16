import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import boto3
from billiard.exceptions import SoftTimeLimitExceeded
from botocore.config import Config
from PIL import Image, UnidentifiedImageError
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
    update,
)
from sqlalchemy import Uuid as SQLUuid

from frame_worker.orchestration.config import WorkerSettings

metadata = MetaData()
runs = Table(
    "annotation_training_runs",
    metadata,
    Column("id", SQLUuid, primary_key=True),
    Column("project_id", SQLUuid),
    Column("snapshot_version", Integer),
    Column("status", String),
    Column("config", JSON),
    Column("selected_image_count", Integer),
    Column("selected_class_count", Integer),
    Column("selected_box_count", Integer),
    Column("progress_completed", Integer),
    Column("progress_total", Integer),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("failure_code", String),
    Column("lease_token", SQLUuid),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("attempt_generation", Integer),
    Column("working_prefix", Text),
    Column("snapshot_artifact_reference", Text),
    Column("snapshot_artifact_size_bytes", BigInteger),
    Column("snapshot_artifact_sha256", String),
    Column("model_artifact_reference", Text),
    Column("model_artifact_size_bytes", BigInteger),
    Column("model_artifact_sha256", String),
    Column("model_version", Integer),
)
images = Table(
    "annotation_training_snapshot_images",
    metadata,
    Column("training_id", SQLUuid),
    Column("image_index", Integer),
    Column("filename", String),
    Column("source_object_key", Text),
    Column("source_size_bytes", BigInteger),
    Column("source_content_type", String),
    Column("source_sha256", String),
    Column("width", Integer),
    Column("height", Integer),
    Column("split", String),
)
classes = Table(
    "annotation_training_snapshot_classes",
    metadata,
    Column("training_id", SQLUuid),
    Column("yolo_index", Integer),
    Column("name", String),
)
boxes = Table(
    "annotation_training_snapshot_boxes",
    metadata,
    Column("training_id", SQLUuid),
    Column("image_index", Integer),
    Column("yolo_index", Integer),
    Column("x_center", Numeric),
    Column("y_center", Numeric),
    Column("width", Numeric),
    Column("height", Numeric),
)
projects = Table(
    "annotation_projects",
    metadata,
    Column("id", SQLUuid, primary_key=True),
    Column("job_id", SQLUuid),
    Column("result_run_token", SQLUuid),
)
jobs = Table(
    "processing_jobs",
    metadata,
    Column("id", SQLUuid, primary_key=True),
    Column("status", String),
    Column("result_reference", Text),
)


class TrainingError(RuntimeError):
    pass


class TrainingLeaseBusy(TrainingError):
    pass


class TrainingLeaseLost(TrainingError):
    pass


class PermanentTrainingError(TrainingError):
    pass


@dataclass(frozen=True)
class Claim:
    row: dict
    token: UUID
    generation: int
    prefix: str


def _claim(engine, training_id: UUID, settings: WorkerSettings) -> Claim | None:
    now, token = datetime.now(UTC), uuid4()
    with engine.begin() as connection:
        row = (
            connection.execute(
                select(runs).where(runs.c.id == training_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["status"] in {"SUCCEEDED", "FAILED", "SNAPSHOT_READY"}:
            return None
        # An expired owner is not proof that its trainer and artifacts were
        # finalized. Only a cleared token denotes an explicit retry-ready run.
        if row["status"] == "RUNNING" and row["lease_token"] is not None:
            return None
        expiry = row["lease_expires_at"]
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry is not None and expiry > now:
            raise TrainingLeaseBusy("Training lease is active")
        generation = int(row["attempt_generation"] or 0) + 1
        prefix = (
            f"annotations/{row['project_id']}/trainings/{training_id}/working/"
            f"{generation}-{token}/"
        )
        connection.execute(
            update(runs)
            .where(runs.c.id == training_id, runs.c.status.in_(("PENDING", "RUNNING")))
            .values(
                status="RUNNING",
                started_at=func.coalesce(runs.c.started_at, now),
                completed_at=None,
                failure_code=None,
                lease_token=token,
                lease_expires_at=now
                + timedelta(seconds=settings.training_lease_seconds),
                attempt_generation=generation,
                working_prefix=prefix,
            )
        )
        return Claim(dict(row), token, generation, prefix)


def _owned(engine, claim: Claim, **values) -> bool:
    with engine.begin() as connection:
        result = connection.execute(
            update(runs)
            .where(
                runs.c.id == claim.row["id"],
                runs.c.status == "RUNNING",
                runs.c.lease_token == claim.token,
                runs.c.attempt_generation == claim.generation,
            )
            .values(**values)
        )
        return result.rowcount == 1


def _heartbeat(
    engine, claim: Claim, settings: WorkerSettings, completed: int | None = None
) -> None:
    values = {
        "lease_expires_at": datetime.now(UTC)
        + timedelta(seconds=settings.training_lease_seconds)
    }
    if completed is not None:
        values["progress_completed"] = completed
    if not _owned(engine, claim, **values):
        raise TrainingLeaseLost("Training lease was lost")


def _sha(path: Path, maximum: int) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise PermanentTrainingError("Artifact limit exceeded")
            digest.update(chunk)
    if size <= 0:
        raise PermanentTrainingError("Empty artifact")
    return size, digest.hexdigest()


def _upload_working_artifact(
    client,
    bucket: str,
    claim: Claim,
    path: Path,
    name: str,
    content_type: str,
    size: int,
    digest: str,
    working_keys: list[str],
) -> str:
    key = claim.prefix + name
    working_keys.append(key)
    try:
        with path.open("rb") as source:
            client.upload_fileobj(
                source,
                bucket,
                key,
                ExtraArgs={"ContentType": content_type, "Metadata": {"sha256": digest}},
            )
        head = client.head_object(Bucket=bucket, Key=key)
        if (
            head["ContentLength"] != size
            or head.get("ContentType") != content_type
            or head.get("Metadata", {}).get("sha256") != digest
        ):
            raise TrainingError("Artifact verification failed")
    except Exception:
        if key.startswith(claim.prefix):
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass
        raise
    return key


def _decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")


def _download_image(client, bucket: str, item: dict, target: Path) -> None:
    if (
        item["source_content_type"] not in {"image/jpeg", "image/png"}
        or item["source_size_bytes"] <= 0
        or item["source_size_bytes"] > 50 * 1024 * 1024
    ):
        raise PermanentTrainingError("Invalid image metadata")
    response = client.get_object(Bucket=bucket, Key=item["source_object_key"])
    if (
        response.get("ContentLength") != item["source_size_bytes"]
        or response.get("ContentType") != item["source_content_type"]
    ):
        response["Body"].close()
        raise PermanentTrainingError("Image metadata changed")
    body = response["Body"]
    digest, size = hashlib.sha256(), 0
    try:
        with target.open("wb") as output:
            while chunk := body.read(1024 * 1024):
                size += len(chunk)
                if size > item["source_size_bytes"]:
                    raise PermanentTrainingError("Image limit exceeded")
                digest.update(chunk)
                output.write(chunk)
    finally:
        body.close()
    if size != item["source_size_bytes"] or digest.hexdigest() != item["source_sha256"]:
        raise PermanentTrainingError("Image integrity failure")
    try:
        with Image.open(target) as decoded:
            decoded.verify()
        with Image.open(target) as decoded:
            if decoded.size != (item["width"], item["height"]):
                raise PermanentTrainingError("Image dimensions changed")
    except (UnidentifiedImageError, OSError) as error:
        raise PermanentTrainingError("Image decode failure") from error


def _snapshot(
    engine, client, bucket: str, claim: Claim, root: Path, settings: WorkerSettings
) -> Path:
    dataset = root / "dataset"
    for relative in ("images/train", "images/val", "labels/train", "labels/val"):
        (dataset / relative).mkdir(parents=True, exist_ok=True)
    with engine.connect() as connection:
        project = (
            connection.execute(
                select(projects).where(projects.c.id == claim.row["project_id"])
            )
            .mappings()
            .one_or_none()
        )
        job = (
            connection.execute(select(jobs).where(jobs.c.id == project["job_id"]))
            .mappings()
            .one_or_none()
            if project is not None
            else None
        )
        image_rows = [
            dict(row)
            for row in connection.execute(
                select(images)
                .where(images.c.training_id == claim.row["id"])
                .order_by(images.c.image_index)
            ).mappings()
        ]
        class_rows = [
            dict(row)
            for row in connection.execute(
                select(classes)
                .where(classes.c.training_id == claim.row["id"])
                .order_by(classes.c.yolo_index)
            ).mappings()
        ]
        box_rows = [
            dict(row)
            for row in connection.execute(
                select(boxes)
                .where(boxes.c.training_id == claim.row["id"])
                .order_by(boxes.c.image_index, boxes.c.yolo_index)
            ).mappings()
        ]
    if project is None or job is None or job["status"] != "SUCCEEDED":
        raise PermanentTrainingError("Snapshot ownership changed")
    source_prefix = f"jobs/{project['job_id']}/results/{project['result_run_token']}/"
    if job["result_reference"] != f"s3://{bucket}/{source_prefix}manifest.json":
        raise PermanentTrainingError("Snapshot source changed")
    if (
        len(image_rows) != claim.row["selected_image_count"]
        or len(class_rows) != claim.row["selected_class_count"]
        or len(box_rows) != claim.row["selected_box_count"]
    ):
        raise PermanentTrainingError("Snapshot rows changed")
    # Source files, the archive, and the bounded model coexist while training.
    # Reserve the artifact limits up front so task-owned tmpfs cannot be exceeded.
    source_bytes = sum(int(item["source_size_bytes"]) for item in image_rows)
    if (
        source_bytes
        + settings.training_max_snapshot_bytes
        + settings.training_max_model_bytes
        > settings.training_max_temp_bytes
    ):
        raise PermanentTrainingError("Training temporary storage limit exceeded")
    labels: dict[int, list[dict]] = {}
    for box in box_rows:
        labels.setdefault(box["image_index"], []).append(box)
    for position, item in enumerate(image_rows, 1):
        if item["split"] not in {"train", "val"}:
            raise PermanentTrainingError("Invalid split")
        if (
            not item["source_object_key"].startswith(source_prefix)
            or ".." in item["source_object_key"].split("/")
            or "\\" in item["source_object_key"]
        ):
            raise PermanentTrainingError("Invalid image mapping")
        extension = ".jpg" if item["source_content_type"] == "image/jpeg" else ".png"
        stem = f"image_{item['image_index']:06d}"
        _download_image(
            client,
            bucket,
            item,
            dataset / "images" / item["split"] / f"{stem}{extension}",
        )
        lines = [
            " ".join(
                (
                    str(box["yolo_index"]),
                    _decimal(box["x_center"]),
                    _decimal(box["y_center"]),
                    _decimal(box["width"]),
                    _decimal(box["height"]),
                )
            )
            for box in labels.get(item["image_index"], [])
        ]
        (dataset / "labels" / item["split"] / f"{stem}.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="ascii"
        )
        if position % 5 == 0:
            _heartbeat(engine, claim, settings, 0)
    names = {int(item["yolo_index"]): item["name"] for item in class_rows}
    (dataset / "data.yaml").write_text(
        json.dumps(
            {
                "path": str(dataset),
                "train": "images/train",
                "val": "images/val",
                "names": names,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    (dataset / "snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "training_id": str(claim.row["id"]),
                "snapshot_version": claim.row["snapshot_version"],
                "image_count": len(image_rows),
                "class_count": len(class_rows),
                "box_count": len(box_rows),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="ascii",
    )
    archive = root / "snapshot.zip"
    with zipfile.ZipFile(
        archive, "w", zipfile.ZIP_DEFLATED, allowZip64=False
    ) as output:
        for path in sorted(dataset.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(dataset).as_posix())
                if archive.stat().st_size > settings.training_max_snapshot_bytes:
                    raise PermanentTrainingError("Snapshot limit exceeded")
    return archive


def _terminate(process: subprocess.Popen) -> None:
    if os.name == "nt":
        if process.poll() is not None:
            return
        process.terminate()
    else:
        # A trainer can exit while a grandchild remains in its session.
        # The session ID is the trainer PID and remains valid until the last
        # member exits, so clean the group even after the leader has exited.
        if process.poll() is None and os.getpgid(process.pid) != process.pid:
            raise TrainingError("Training process isolation failure")
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            raise TrainingError("Training process cleanup failure") from error
    if os.name != "nt":
        # Waiting for the leader does not wait for its descendants.
        time.sleep(0.5)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while _group_alive(process.pid):
            if time.monotonic() >= deadline:
                raise TrainingError("Training process cleanup failure")
            time.sleep(0.05)


def _group_alive(group_id: int) -> bool:
    if sys.platform.startswith("linux"):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
                if int(fields[2]) == group_id and fields[0] != "Z":
                    return True
            except (FileNotFoundError, PermissionError, ValueError, IndexError):
                continue
        return False
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _training_root(base: Path, claim: Claim) -> Path:
    # UUID and integer components keep cleanup confined to this lease attempt.
    attempt_root = (
        base.resolve()
        / "annotation-training"
        / str(UUID(str(claim.row["id"])))
        / f"{claim.generation}-{UUID(str(claim.token))}"
    )
    # The trainer resolves PROCESSING_TEMP_ROOT/annotation-training/<id>.
    return attempt_root / "annotation-training" / str(UUID(str(claim.row["id"])))


def _training_platform_supported() -> bool:
    return os.name == "posix" and sys.platform.startswith("linux")


def _attempt_root(root: Path, claim: Claim) -> Path:
    attempt_root = root.parent.parent
    training_id = str(UUID(str(claim.row["id"])))
    token = str(UUID(str(claim.token)))
    if (
        root != attempt_root / "annotation-training" / training_id
        or attempt_root.name != f"{claim.generation}-{token}"
        or attempt_root.parent.name != training_id
        or attempt_root.parent.parent.name != "annotation-training"
    ):
        raise TrainingError("Unsafe training workspace")
    return attempt_root


def _copy_final_artifact(
    client,
    bucket: str,
    claim: Claim,
    name: str,
    key: str,
    content_type: str,
    size: int,
    digest: str,
    uploaded: list[str],
) -> None:
    uploaded.append(key)
    client.copy_object(
        Bucket=bucket,
        Key=key,
        CopySource={"Bucket": bucket, "Key": claim.prefix + name},
        ContentType=content_type,
        Metadata={"sha256": digest},
        MetadataDirective="REPLACE",
    )
    head = client.head_object(Bucket=bucket, Key=key)
    if (
        head["ContentLength"] != size
        or head.get("ContentType") != content_type
        or head.get("Metadata", {}).get("sha256") != digest
    ):
        raise TrainingError("Final artifact verification failed")


def _cleanup_training(
    process: subprocess.Popen | None,
    client,
    bucket: str,
    claim: Claim,
    uploaded: list[str],
    working_keys: list[str],
    root: Path,
    engine=None,
) -> None:
    if process is not None:
        try:
            _terminate(process)
        except Exception:
            # A running trainer must never lose its artifacts or workspace.
            raise TrainingError("Training process cleanup failure") from None
    attempt_root = _attempt_root(root, claim)
    final_cleanup_failed = False
    if uploaded:
        try:
            if engine is None:
                current_generation = claim.generation
                connection = None
            else:
                connection = engine.begin()
            if connection is None:
                final_cleanup_failed = _delete_uploaded(
                    client, bucket, claim, uploaded, current_generation
                )
            else:
                with connection as locked:
                    locked.execute(
                        select(projects.c.id)
                        .where(projects.c.id == claim.row["project_id"])
                        .with_for_update()
                    )
                    current_generation = locked.scalar(
                        select(runs.c.attempt_generation)
                        .where(runs.c.id == claim.row["id"])
                        .with_for_update()
                    )
                    if current_generation is None:
                        final_cleanup_failed = True
                    else:
                        final_cleanup_failed = _delete_uploaded(
                            client, bucket, claim, uploaded, current_generation
                        )
        except Exception:
            final_cleanup_failed = True
    working_cleanup_failed = False
    for key in working_keys:
        if key.startswith(claim.prefix):
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                working_cleanup_failed = True
    if attempt_root.exists():
        try:
            shutil.rmtree(attempt_root)
        except Exception:
            raise TrainingError("Training temporary cleanup failure") from None
    if final_cleanup_failed:
        raise TrainingError("Final artifact cleanup failure") from None
    if working_cleanup_failed:
        raise TrainingError("Working artifact cleanup failure") from None


def _delete_uploaded(client, bucket, claim, uploaded, current_generation) -> bool:
    if current_generation != claim.generation:
        return False
    failed = False
    prefix = f"annotations/{claim.row['project_id']}/trainings/{claim.row['id']}/"
    for key in uploaded:
        if key.startswith(prefix):
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                failed = True
    return failed


def _finalize_failed_attempt(
    engine,
    client,
    bucket: str,
    claim: Claim,
    process: subprocess.Popen | None,
    uploaded: list[str],
    working_keys: list[str],
    root: Path,
    failure_code: str | None,
) -> None:
    cleanup_error: TrainingError | None = None
    cleanup_started = False
    try:
        with engine.begin() as connection:
            connection.execute(
                select(projects.c.id)
                .where(projects.c.id == claim.row["project_id"])
                .with_for_update()
            )
            current = connection.execute(
                select(runs.c.status, runs.c.lease_token, runs.c.attempt_generation)
                .where(runs.c.id == claim.row["id"])
                .with_for_update()
            ).one_or_none()
            owned = current == ("RUNNING", claim.token, claim.generation)
            cleanup_started = True
            try:
                _cleanup_training(
                    process,
                    client,
                    bucket,
                    claim,
                    uploaded if owned else [],
                    working_keys,
                    root,
                )
            except TrainingError as error:
                cleanup_error = error
            if owned:
                values = {
                    "lease_token": None,
                    "lease_expires_at": None,
                    "working_prefix": None,
                }
                if cleanup_error is not None:
                    values.update(
                        status="FAILED",
                        completed_at=datetime.now(UTC),
                        failure_code="TRAINING_CLEANUP_FAILED",
                    )
                elif failure_code is not None:
                    values.update(
                        status="FAILED",
                        completed_at=datetime.now(UTC),
                        failure_code=failure_code,
                    )
                result = connection.execute(
                    update(runs)
                    .where(
                        runs.c.id == claim.row["id"],
                        runs.c.status == "RUNNING",
                        runs.c.lease_token == claim.token,
                        runs.c.attempt_generation == claim.generation,
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise TrainingLeaseLost("Training lease was lost")
    except Exception:
        if not cleanup_started and process is not None:
            try:
                _terminate(process)
            except Exception:
                raise TrainingError("Training process cleanup failure") from None
        raise TrainingError("Training finalization dependency failure") from None
    if cleanup_error is not None:
        raise TrainingError(str(cleanup_error)) from None


def train_annotation_model(training_id: UUID, settings: WorkerSettings) -> bool:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    client = boto3.client(
        "s3",
        endpoint_url=settings.object_storage_endpoint,
        aws_access_key_id=settings.object_storage_access_key,
        aws_secret_access_key=settings.object_storage_secret_key,
        region_name=settings.object_storage_region,
        config=Config(
            s3={"addressing_style": settings.object_storage_addressing_style},
            connect_timeout=5,
            read_timeout=30,
            retries={"total_max_attempts": 2, "mode": "standard"},
        ),
    )
    claim = _claim(engine, training_id, settings)
    if claim is None:
        engine.dispose()
        return False
    base = settings.processing_temp_root or Path("/tmp/frame-intelligence")
    root = _training_root(base, claim)
    uploaded: list[str] = []
    working_keys: list[str] = []
    process: subprocess.Popen | None = None
    failure_code: str | None = None
    succeeded = False
    try:
        if not _training_platform_supported():
            raise PermanentTrainingError("Unsupported training platform")
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        snapshot = _snapshot(
            engine, client, settings.object_storage_bucket, claim, root, settings
        )
        snapshot_size, snapshot_sha = _sha(
            snapshot, settings.training_max_snapshot_bytes
        )
        _upload_working_artifact(
            client,
            settings.object_storage_bucket,
            claim,
            snapshot,
            "snapshot.zip",
            "application/zip",
            snapshot_size,
            snapshot_sha,
            working_keys,
        )
        config = claim.row["config"]
        epochs, batch = int(config.get("epochs", 10)), int(config.get("batch_size", 2))
        if (
            not 1 <= epochs <= 25
            or not 1 <= batch <= 4
            or not settings.yolo_model_path.is_file()
        ):
            raise PermanentTrainingError("Invalid training configuration")
        (root / "launch.json").write_text(
            json.dumps(
                {
                    "model": str(settings.yolo_model_path),
                    "epochs": epochs,
                    "batch_size": batch,
                },
                separators=(",", ":"),
            ),
            encoding="ascii",
        )
        expected_parent_pid = os.getpid()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "frame_worker.orchestration.yolo_trainer",
                str(training_id),
            ],
            shell=False,
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            env={
                **os.environ,
                "TRAINING_PARENT_PID": str(expected_parent_pid),
                "PROCESSING_TEMP_ROOT": str(root.parent.parent),
            },
        )
        started = time.monotonic()
        while process.poll() is None:
            if time.monotonic() - started > settings.training_timeout_seconds:
                raise PermanentTrainingError("Training timed out")
            results_csv = root / "runs" / "train" / "results.csv"
            completed = 0
            if results_csv.is_file():
                completed = min(
                    epochs,
                    max(
                        0, len(results_csv.read_text(encoding="utf-8").splitlines()) - 1
                    ),
                )
            _heartbeat(engine, claim, settings, completed)
            time.sleep(min(settings.training_heartbeat_interval_seconds, 5))
        _terminate(process)
        returncode = process.returncode
        process = None  # The complete process group has already been verified dead.
        if returncode != 0:
            raise PermanentTrainingError("Training process failed")
        model = root / "runs" / "train" / "weights" / "best.pt"
        model_size, model_sha = _sha(model, settings.training_max_model_bytes)
        final_prefix = f"annotations/{claim.row['project_id']}/trainings/{training_id}/"
        model_key, snapshot_key = (
            final_prefix + "model.pt",
            final_prefix + "snapshot.zip",
        )
        artifacts = (
            (
                model,
                "model.pt",
                model_key,
                "application/octet-stream",
                model_size,
                model_sha,
            ),
            (
                snapshot,
                "snapshot.zip",
                snapshot_key,
                "application/zip",
                snapshot_size,
                snapshot_sha,
            ),
        )
        for path, name, _final_key, content_type, size, digest in artifacts[:1]:
            _upload_working_artifact(
                client,
                settings.object_storage_bucket,
                claim,
                path,
                name,
                content_type,
                size,
                digest,
                working_keys,
            )
        with engine.begin() as connection:
            connection.execute(
                select(projects.c.id)
                .where(projects.c.id == claim.row["project_id"])
                .with_for_update()
            )
            current = connection.execute(
                select(runs.c.status, runs.c.lease_token, runs.c.attempt_generation)
                .where(runs.c.id == training_id)
                .with_for_update()
            ).one()
            if current != ("RUNNING", claim.token, claim.generation):
                raise TrainingLeaseLost("Training lease was lost")
            # The row lock fences every stable final copy against the next claim.
            for _path, name, key, content_type, size, digest in artifacts:
                _copy_final_artifact(
                    client,
                    settings.object_storage_bucket,
                    claim,
                    name,
                    key,
                    content_type,
                    size,
                    digest,
                    uploaded,
                )
            # The lease and row locks remain held until working and temp
            # cleanup completes; only then may the run become successful.
            _cleanup_training(
                None,
                client,
                settings.object_storage_bucket,
                claim,
                [],
                working_keys,
                root,
            )
            version = (
                int(
                    connection.scalar(
                        select(func.coalesce(func.max(runs.c.model_version), 0)).where(
                            runs.c.project_id == claim.row["project_id"]
                        )
                    )
                    or 0
                )
                + 1
            )
            result = connection.execute(
                update(runs)
                .where(
                    runs.c.id == training_id,
                    runs.c.status == "RUNNING",
                    runs.c.lease_token == claim.token,
                    runs.c.attempt_generation == claim.generation,
                )
                .values(
                    status="SUCCEEDED",
                    progress_completed=epochs,
                    progress_total=epochs,
                    completed_at=datetime.now(UTC),
                    failure_code=None,
                    lease_token=None,
                    lease_expires_at=None,
                    working_prefix=None,
                    model_version=version,
                    snapshot_artifact_reference=f"s3://{settings.object_storage_bucket}/{snapshot_key}",
                    snapshot_artifact_size_bytes=snapshot_size,
                    snapshot_artifact_sha256=snapshot_sha,
                    model_artifact_reference=f"s3://{settings.object_storage_bucket}/{model_key}",
                    model_artifact_size_bytes=model_size,
                    model_artifact_sha256=model_sha,
                )
            )
            if result.rowcount != 1:
                raise TrainingLeaseLost("Training lease was lost")
        uploaded.clear()
        working_keys.clear()
        succeeded = True
        return True
    except SoftTimeLimitExceeded as error:
        failure_code = "TRAINING_TIMEOUT"
        raise PermanentTrainingError("Training timed out") from error
    except PermanentTrainingError as error:
        failure_code = (
            "TRAINING_UNSUPPORTED_PLATFORM"
            if str(error) == "Unsupported training platform"
            else "TRAINING_TIMEOUT"
            if str(error) == "Training timed out"
            else "TRAINING_FAILED"
        )
        raise
    except TrainingLeaseLost:
        raise
    except Exception as error:
        if isinstance(error, TrainingError):
            raise
        raise TrainingError("Temporary training dependency failure") from error
    finally:
        try:
            if not succeeded:
                _finalize_failed_attempt(
                    engine,
                    client,
                    settings.object_storage_bucket,
                    claim,
                    process,
                    uploaded,
                    working_keys,
                    root,
                    failure_code,
                )
        finally:
            engine.dispose()


def fail_unleased_training(training_id: UUID, settings: WorkerSettings) -> None:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                update(runs)
                .where(
                    runs.c.id == training_id,
                    runs.c.status.in_(("PENDING", "RUNNING")),
                    (runs.c.lease_token.is_(None)) | (runs.c.lease_expires_at <= now),
                )
                .values(
                    status="FAILED",
                    completed_at=now,
                    failure_code="TRAINING_RETRY_EXHAUSTED",
                    lease_token=None,
                    lease_expires_at=None,
                    working_prefix=None,
                )
            )
    finally:
        engine.dispose()
