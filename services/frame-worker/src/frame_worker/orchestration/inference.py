import hashlib
import math
import shutil
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from PIL import Image, UnidentifiedImageError
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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
    insert,
    select,
    update,
)
from sqlalchemy import Uuid as SQLUuid

from frame_worker.orchestration.config import WorkerSettings

metadata = MetaData()
inference_runs = Table(
    "annotation_inference_runs",
    metadata,
    Column("id", SQLUuid, primary_key=True),
    Column("project_id", SQLUuid),
    Column("training_id", SQLUuid),
    Column("model_version", Integer),
    Column("status", String),
    Column("targets", JSON),
    Column("target_image_count", Integer),
    Column("processed_image_count", Integer),
    Column("created_box_count", Integer),
    Column("created_at", DateTime(timezone=True)),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("failure_code", String),
)
training_runs = Table(
    "annotation_training_runs",
    metadata,
    Column("id", SQLUuid, primary_key=True),
    Column("project_id", SQLUuid),
    Column("status", String),
    Column("model_artifact_reference", Text),
    Column("model_artifact_size_bytes", BigInteger),
    Column("model_artifact_sha256", String),
    Column("model_version", Integer),
)
snapshot_classes = Table(
    "annotation_training_snapshot_classes",
    metadata,
    Column("training_id", SQLUuid),
    Column("project_id", SQLUuid),
    Column("class_id", SQLUuid),
    Column("yolo_index", Integer),
)
annotation_classes = Table(
    "annotation_classes",
    metadata,
    Column("id", SQLUuid),
    Column("project_id", SQLUuid),
)
annotation_images = Table(
    "annotation_images",
    metadata,
    Column("project_id", SQLUuid),
    Column("image_index", Integer),
    Column("completed", Boolean),
    Column("updated_at", DateTime(timezone=True)),
)
annotation_boxes = Table(
    "annotation_boxes",
    metadata,
    Column("id", SQLUuid),
    Column("project_id", SQLUuid),
    Column("image_index", Integer),
    Column("class_id", SQLUuid),
    Column("auto_label_run_id", SQLUuid),
    Column("x_center", Numeric(9, 8)),
    Column("y_center", Numeric(9, 8)),
    Column("width", Numeric(9, 8)),
    Column("height", Numeric(9, 8)),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)
annotation_projects = Table(
    "annotation_projects",
    metadata,
    Column("id", SQLUuid, primary_key=True),
    Column("revision", BigInteger),
    Column("updated_at", DateTime(timezone=True)),
)


class InferenceError(RuntimeError):
    pass


class PermanentInferenceError(InferenceError):
    pass


_REQUIRED_TARGET_KEYS = {
    "image_index",
    "source_object_key",
    "source_size_bytes",
    "source_content_type",
    "source_sha256",
    "width",
    "height",
}
_Q = Decimal("0.00000001")
MAX_BOXES_PER_PROJECT = 50_000


def _s3_client(settings: WorkerSettings):
    return boto3.client(
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


def _validated_targets(raw_targets: object, expected_count: int) -> list[dict]:
    if not isinstance(raw_targets, list) or len(raw_targets) != expected_count:
        raise PermanentInferenceError("Invalid inference target set")
    targets: list[dict] = []
    seen: set[int] = set()
    for raw in raw_targets:
        if not isinstance(raw, dict) or set(raw) != _REQUIRED_TARGET_KEYS:
            raise PermanentInferenceError("Invalid inference target")
        try:
            index = int(raw["image_index"])
            size = int(raw["source_size_bytes"])
            width = int(raw["width"])
            height = int(raw["height"])
        except (TypeError, ValueError) as error:
            raise PermanentInferenceError(
                "Invalid inference target metadata"
            ) from error
        key = raw["source_object_key"]
        content_type = raw["source_content_type"]
        digest = raw["source_sha256"]
        if (
            index < 0
            or index in seen
            or size <= 0
            or size > 50 * 1024 * 1024
            or width <= 0
            or height <= 0
            or not isinstance(key, str)
            or not key
            or "\\" in key
            or ".." in key.split("/")
            or content_type not in {"image/jpeg", "image/png"}
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise PermanentInferenceError("Invalid inference target metadata")
        seen.add(index)
        targets.append(
            {
                "image_index": index,
                "source_object_key": key,
                "source_size_bytes": size,
                "source_content_type": content_type,
                "source_sha256": digest,
                "width": width,
                "height": height,
            }
        )
    return targets


def _download_image(client, bucket: str, item: dict, target: Path) -> None:
    response = client.get_object(Bucket=bucket, Key=item["source_object_key"])
    if (
        response.get("ContentLength") != item["source_size_bytes"]
        or response.get("ContentType") != item["source_content_type"]
    ):
        response["Body"].close()
        raise PermanentInferenceError("Image metadata changed")
    body = response["Body"]
    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := body.read(1024 * 1024):
                size += len(chunk)
                if size > item["source_size_bytes"]:
                    raise PermanentInferenceError("Image limit exceeded")
                digest.update(chunk)
                output.write(chunk)
    finally:
        body.close()
    if size != item["source_size_bytes"] or digest.hexdigest() != item["source_sha256"]:
        raise PermanentInferenceError("Image integrity failure")
    try:
        with Image.open(target) as decoded:
            decoded.verify()
        with Image.open(target) as decoded:
            if decoded.size != (item["width"], item["height"]):
                raise PermanentInferenceError("Image dimensions changed")
    except (UnidentifiedImageError, OSError) as error:
        raise PermanentInferenceError("Image decode failure") from error


def _download_model(client, bucket: str, row: dict, target: Path, maximum: int) -> None:
    training_id = UUID(str(row["training_id"]))
    project_id = UUID(str(row["project_id"]))
    expected_key = f"annotations/{project_id}/trainings/{training_id}/model.pt"
    expected_reference = f"s3://{bucket}/{expected_key}"
    if row["model_artifact_reference"] != expected_reference:
        raise PermanentInferenceError("Invalid model artifact reference")
    expected_size = row["model_artifact_size_bytes"]
    expected_sha = row["model_artifact_sha256"]
    if (
        not isinstance(expected_size, int)
        or expected_size <= 0
        or expected_size > maximum
        or not isinstance(expected_sha, str)
        or len(expected_sha) != 64
    ):
        raise PermanentInferenceError("Invalid model artifact metadata")
    response = client.get_object(Bucket=bucket, Key=expected_key)
    if (
        response.get("ContentLength") != expected_size
        or response.get("ContentType") != "application/octet-stream"
        or response.get("Metadata", {}).get("sha256") != expected_sha
    ):
        response["Body"].close()
        raise PermanentInferenceError("Model artifact metadata changed")
    digest = hashlib.sha256()
    size = 0
    body = response["Body"]
    try:
        with target.open("wb") as output:
            while chunk := body.read(1024 * 1024):
                size += len(chunk)
                if size > expected_size:
                    raise PermanentInferenceError("Model artifact limit exceeded")
                digest.update(chunk)
                output.write(chunk)
    finally:
        body.close()
    if size != expected_size or digest.hexdigest() != expected_sha:
        raise PermanentInferenceError("Model artifact integrity failure")


def _decimal(value: float) -> Decimal:
    try:
        if not math.isfinite(value):
            raise PermanentInferenceError("Invalid prediction coordinate")
        return Decimal(str(value)).quantize(_Q)
    except (InvalidOperation, ValueError) as error:
        raise PermanentInferenceError("Invalid prediction coordinate") from error


def _prediction_rows(result, class_ids: dict[int, UUID]) -> list[dict]:
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []
    xyxyn = boxes.xyxyn.detach().cpu().tolist()
    class_values = boxes.cls.detach().cpu().tolist()
    confidence_values = boxes.conf.detach().cpu().tolist()
    ranked = sorted(
        zip(confidence_values, class_values, xyxyn, strict=True),
        key=lambda item: float(item[0]),
        reverse=True,
    )[:200]
    rows: list[dict] = []
    epsilon = 1e-7
    for confidence, raw_class, raw_box in ranked:
        if (
            not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
            or not math.isfinite(float(raw_class))
            or float(raw_class) != int(raw_class)
        ):
            raise PermanentInferenceError("Invalid prediction metadata")
        yolo_index = int(raw_class)
        class_id = class_ids.get(yolo_index)
        if class_id is None or len(raw_box) != 4:
            raise PermanentInferenceError("Invalid prediction class or box")
        x1, y1, x2, y2 = (float(value) for value in raw_box)
        if not all(
            math.isfinite(value) and 0 <= value <= 1 for value in (x1, y1, x2, y2)
        ):
            raise PermanentInferenceError("Invalid prediction coordinate")
        x1 = min(1.0 - epsilon, max(epsilon, x1))
        y1 = min(1.0 - epsilon, max(epsilon, y1))
        x2 = min(1.0 - epsilon, max(epsilon, x2))
        y2 = min(1.0 - epsilon, max(epsilon, y2))
        if x2 <= x1 or y2 <= y1:
            raise PermanentInferenceError("Invalid prediction bounds")
        width = _decimal(x2 - x1)
        height = _decimal(y2 - y1)
        x_center = _decimal((x1 + x2) / 2)
        y_center = _decimal((y1 + y2) / 2)
        if width <= 0 or height <= 0:
            continue
        rows.append(
            {
                "class_id": class_id,
                "x_center": x_center,
                "y_center": y_center,
                "width": width,
                "height": height,
            }
        )
    return rows


def _write_predictions(
    engine,
    run_id: UUID,
    project_id: UUID,
    image_index: int,
    rows: list[dict],
    generation: datetime,
) -> int:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        active = connection.execute(
            select(inference_runs.c.id)
            .where(
                inference_runs.c.id == run_id,
                inference_runs.c.project_id == project_id,
                inference_runs.c.status == "RUNNING",
                inference_runs.c.started_at == generation,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if active is None:
            return 0
        project = (
            connection.execute(
                select(annotation_projects)
                .where(annotation_projects.c.id == project_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if project is None:
            return 0
        image = (
            connection.execute(
                select(annotation_images)
                .where(
                    annotation_images.c.project_id == project_id,
                    annotation_images.c.image_index == image_index,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if image is None or image["completed"]:
            return 0
        existing = int(
            connection.scalar(
                select(func.count())
                .select_from(annotation_boxes)
                .where(
                    annotation_boxes.c.project_id == project_id,
                    annotation_boxes.c.image_index == image_index,
                )
            )
            or 0
        )
        if existing:
            return 0
        current_classes = set(
            connection.scalars(
                select(annotation_classes.c.id).where(
                    annotation_classes.c.project_id == project_id
                )
            ).all()
        )
        project_box_count = int(
            connection.scalar(
                select(func.count())
                .select_from(annotation_boxes)
                .where(annotation_boxes.c.project_id == project_id)
            )
            or 0
        )
        remaining = max(0, MAX_BOXES_PER_PROJECT - project_box_count)
        if remaining == 0:
            return 0
        inserts = []
        for row in rows:
            if row["class_id"] not in current_classes:
                continue
            inserts.append(
                {
                    "id": uuid4(),
                    "project_id": project_id,
                    "image_index": image_index,
                    "class_id": row["class_id"],
                    "auto_label_run_id": run_id,
                    "x_center": row["x_center"],
                    "y_center": row["y_center"],
                    "width": row["width"],
                    "height": row["height"],
                    "created_at": now,
                    "updated_at": now,
                }
            )
            if len(inserts) >= remaining:
                break
        if not inserts:
            return 0
        connection.execute(insert(annotation_boxes), inserts)
        connection.execute(
            update(annotation_images)
            .where(
                annotation_images.c.project_id == project_id,
                annotation_images.c.image_index == image_index,
            )
            .values(updated_at=now)
        )
        connection.execute(
            update(annotation_projects)
            .where(annotation_projects.c.id == project_id)
            .values(
                revision=annotation_projects.c.revision + 1,
                updated_at=now,
            )
        )
        return len(inserts)


def _box_count(engine, run_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                select(func.count())
                .select_from(annotation_boxes)
                .where(annotation_boxes.c.auto_label_run_id == run_id)
            )
            or 0
        )


def fail_inference(
    run_id: UUID,
    settings: WorkerSettings,
    code: str,
    generation: datetime | None = None,
) -> None:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            claim = (
                inference_runs.c.started_at == generation
                if generation is not None
                else inference_runs.c.status == "PENDING"
            )
            connection.execute(
                update(inference_runs)
                .where(
                    inference_runs.c.id == run_id,
                    inference_runs.c.status.in_(("PENDING", "RUNNING")),
                    claim,
                )
                .values(
                    status="FAILED",
                    completed_at=datetime.now(UTC),
                    failure_code=code[:64],
                    created_box_count=select(func.count())
                    .select_from(annotation_boxes)
                    .where(annotation_boxes.c.auto_label_run_id == run_id)
                    .scalar_subquery(),
                )
            )
    finally:
        engine.dispose()


def run_annotation_inference(run_id: UUID, settings: WorkerSettings) -> bool:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    client = _s3_client(settings)
    base = settings.processing_temp_root or Path("/tmp/frame-intelligence")
    root = base.resolve() / "annotation-inference" / str(run_id) / str(uuid4())
    generation: datetime | None = None
    try:
        with engine.begin() as connection:
            run = (
                connection.execute(
                    select(inference_runs)
                    .where(inference_runs.c.id == run_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if run is None or run["status"] in {"SUCCEEDED", "FAILED"}:
                return False
            if run["status"] not in {"PENDING", "RUNNING"}:
                raise PermanentInferenceError("Invalid inference state")
            generation = datetime.now(UTC)
            connection.execute(
                update(inference_runs)
                .where(inference_runs.c.id == run_id)
                .values(
                    status="RUNNING",
                    started_at=generation,
                    completed_at=None,
                    failure_code=None,
                )
            )
            run_data = dict(run)
        targets = _validated_targets(
            run_data["targets"], int(run_data["target_image_count"])
        )
        project_id = UUID(str(run_data["project_id"]))
        training_id = UUID(str(run_data["training_id"]))
        with engine.connect() as connection:
            training = (
                connection.execute(
                    select(training_runs).where(
                        training_runs.c.id == training_id,
                        training_runs.c.project_id == project_id,
                        training_runs.c.status == "SUCCEEDED",
                        training_runs.c.model_version == run_data["model_version"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            class_rows = connection.execute(
                select(
                    snapshot_classes.c.yolo_index,
                    snapshot_classes.c.class_id,
                ).where(
                    snapshot_classes.c.training_id == training_id,
                    snapshot_classes.c.project_id == project_id,
                )
            ).all()
        if training is None or not class_rows:
            raise PermanentInferenceError("Trained model is unavailable")
        class_ids = {int(row.yolo_index): UUID(str(row.class_id)) for row in class_rows}

        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        model_path = root / "model.pt"
        _download_model(
            client,
            settings.object_storage_bucket,
            {**dict(training), "training_id": training_id, "project_id": project_id},
            model_path,
            settings.training_max_model_bytes,
        )
        try:
            from ultralytics import YOLO
        except ImportError as error:
            raise PermanentInferenceError("Ultralytics is unavailable") from error
        model = YOLO(str(model_path))

        for position, target in enumerate(targets, start=1):
            suffix = ".jpg" if target["source_content_type"] == "image/jpeg" else ".png"
            image_path = root / f"image-{target['image_index']:06d}{suffix}"
            _download_image(
                client,
                settings.object_storage_bucket,
                target,
                image_path,
            )
            try:
                predictions = model.predict(
                    source=str(image_path),
                    imgsz=640,
                    conf=0.25,
                    device="cpu",
                    verbose=False,
                    max_det=200,
                )
            except Exception as error:
                raise InferenceError("YOLO inference failed") from error
            if len(predictions) != 1:
                raise PermanentInferenceError("Unexpected inference result")
            rows = _prediction_rows(predictions[0], class_ids)
            _write_predictions(
                engine,
                run_id,
                project_id,
                int(target["image_index"]),
                rows,
                generation,
            )
            try:
                image_path.unlink()
            except FileNotFoundError:
                pass
            with engine.begin() as connection:
                connection.execute(
                    update(inference_runs)
                    .where(
                        inference_runs.c.id == run_id,
                        inference_runs.c.status == "RUNNING",
                        inference_runs.c.started_at == generation,
                    )
                    .values(
                        processed_image_count=func.greatest(
                            inference_runs.c.processed_image_count, position
                        ),
                        created_box_count=select(func.count())
                        .select_from(annotation_boxes)
                        .where(annotation_boxes.c.auto_label_run_id == run_id)
                        .scalar_subquery(),
                    )
                )

        with engine.begin() as connection:
            result = connection.execute(
                update(inference_runs)
                .where(
                    inference_runs.c.id == run_id,
                    inference_runs.c.status == "RUNNING",
                    inference_runs.c.started_at == generation,
                )
                .values(
                    status="SUCCEEDED",
                    processed_image_count=len(targets),
                    created_box_count=select(func.count())
                    .select_from(annotation_boxes)
                    .where(annotation_boxes.c.auto_label_run_id == run_id)
                    .scalar_subquery(),
                    completed_at=datetime.now(UTC),
                    failure_code=None,
                )
            )
            if result.rowcount != 1:
                raise InferenceError("Inference state changed")
        return True
    except Exception as error:
        error.inference_generation = generation
        raise
    finally:
        try:
            if root.exists() and root.parent.parent.name == "annotation-inference":
                shutil.rmtree(root)
                try:
                    root.parent.rmdir()
                except OSError:
                    pass
        finally:
            engine.dispose()
