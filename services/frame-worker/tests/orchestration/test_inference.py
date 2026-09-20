import base64
import hashlib
import io
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import create_engine, insert, select

os.environ.setdefault(
    "JOB_SOURCE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"T" * 32).decode()
)

from frame_worker.orchestration import tasks  # noqa: F401
from frame_worker.orchestration.celery_app import celery_app
from frame_worker.orchestration.inference import (
    PermanentInferenceError,
    _download_image,
    _download_model,
    _prediction_rows,
    _validated_targets,
    _write_predictions,
    annotation_boxes,
    annotation_classes,
    annotation_images,
    annotation_projects,
    fail_inference,
    inference_runs,
    metadata,
)


class Tensor:
    def __init__(self, value):
        self.value = value

    def detach(self):
        return self

    def cpu(self):
        return self

    def tolist(self):
        return self.value


def prediction(box, index=0, confidence=0.9):
    class Boxes(SimpleNamespace):
        def __len__(self):
            return 1

    return SimpleNamespace(
        boxes=Boxes(xyxyn=Tensor([box]), cls=Tensor([index]), conf=Tensor([confidence]))
    )


def test_task_routes_to_ml_queue():
    task = celery_app.tasks["frame_worker.auto_label_annotations"]
    assert task.name == "frame_worker.auto_label_annotations"
    assert celery_app.conf.task_routes[task.name] == {"queue": "annotation-ml"}


def test_prediction_coordinates_and_class_mapping():
    class_id = uuid4()
    rows = _prediction_rows(prediction([0.1, 0.2, 0.5, 0.8]), {0: class_id})
    assert len(rows) == 1
    assert rows[0]["class_id"] == class_id
    assert tuple(
        str(rows[0][key]) for key in ("x_center", "y_center", "width", "height")
    ) == ("0.30000000", "0.50000000", "0.40000000", "0.60000000")


@pytest.mark.parametrize(
    "box,index,confidence",
    [
        ([float("nan"), 0, 1, 1], 0, 0.9),
        ([-0.1, 0, 1, 1], 0, 0.9),
        ([0.8, 0, 0.2, 1], 0, 0.9),
        ([0, 0, 1, 1], 2, 0.9),
        ([0, 0, 1, 1], 0, float("nan")),
    ],
)
def test_bad_predictions_fail_closed(box, index, confidence):
    with pytest.raises(PermanentInferenceError):
        _prediction_rows(prediction(box, index, confidence), {0: uuid4()})


def test_target_metadata_rejects_bad_hash_and_path():
    target = {
        "image_index": 0,
        "source_object_key": "jobs/one/image.png",
        "source_size_bytes": 4,
        "source_content_type": "image/png",
        "source_sha256": "a" * 64,
        "width": 2,
        "height": 2,
    }
    assert _validated_targets([target], 1)[0]["image_index"] == 0
    for change in (
        {"source_sha256": "bad"},
        {"source_object_key": "../secret"},
        {"source_content_type": "text/plain"},
        {"source_size_bytes": 0},
    ):
        with pytest.raises(PermanentInferenceError):
            _validated_targets([{**target, **change}], 1)


def test_model_metadata_and_digest(tmp_path):
    project_id, training_id = uuid4(), uuid4()
    payload = b"model-content"
    digest = hashlib.sha256(payload).hexdigest()
    key = f"annotations/{project_id}/trainings/{training_id}/model.pt"
    row = {
        "project_id": project_id,
        "training_id": training_id,
        "model_artifact_reference": f"s3://bucket/{key}",
        "model_artifact_size_bytes": len(payload),
        "model_artifact_sha256": digest,
    }

    class Client:
        def __init__(self, body=payload, content_type="application/octet-stream"):
            self.body = body
            self.content_type = content_type

        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "bucket", "Key": key}
            return {
                "Body": io.BytesIO(self.body),
                "ContentLength": len(self.body),
                "ContentType": self.content_type,
                "Metadata": {"sha256": digest},
            }

    target = tmp_path / "model.pt"
    _download_model(Client(), "bucket", row, target, 100)
    assert target.read_bytes() == payload
    with pytest.raises(PermanentInferenceError):
        _download_model(Client(body=b"wrong-content"), "bucket", row, target, 100)
    with pytest.raises(PermanentInferenceError):
        _download_model(Client(content_type="text/plain"), "bucket", row, target, 100)


def test_source_image_metadata_and_digest(tmp_path):
    output = io.BytesIO()
    Image.new("RGB", (2, 3), "white").save(output, format="PNG")
    payload = output.getvalue()
    target = {
        "source_object_key": "jobs/image.png",
        "source_size_bytes": len(payload),
        "source_content_type": "image/png",
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "width": 2,
        "height": 3,
    }

    class Client:
        def __init__(self, body=payload, content_type="image/png"):
            self.body = body
            self.content_type = content_type

        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "bucket", "Key": "jobs/image.png"}
            return {
                "Body": io.BytesIO(self.body),
                "ContentLength": len(self.body),
                "ContentType": self.content_type,
            }

    path = tmp_path / "image.png"
    _download_image(Client(), "bucket", target, path)
    assert path.read_bytes() == payload
    with pytest.raises(PermanentInferenceError):
        _download_image(Client(content_type="text/plain"), "bucket", target, path)
    with pytest.raises(PermanentInferenceError):
        _download_image(Client(body=payload + b"x"), "bucket", target, path)


def test_old_generation_cannot_write_predictions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'inference.db'}")
    metadata.create_all(engine)
    run_id, project_id = uuid4(), uuid4()
    current = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(inference_runs).values(
                id=run_id,
                project_id=project_id,
                training_id=uuid4(),
                model_version=1,
                status="RUNNING",
                targets=[],
                target_image_count=1,
                processed_image_count=0,
                created_box_count=0,
                created_at=current,
                started_at=current,
            )
        )
    assert (
        _write_predictions(
            engine, run_id, project_id, 0, [], current - timedelta(seconds=1)
        )
        == 0
    )
    settings = SimpleNamespace(database_url=f"sqlite:///{tmp_path / 'inference.db'}")
    fail_inference(run_id, settings, "STALE", current - timedelta(seconds=1))
    with engine.connect() as connection:
        assert connection.scalar(select(inference_runs.c.status)) == "RUNNING"
        assert connection.scalar(select(annotation_boxes.c.id)) is None
    fail_inference(run_id, settings, "INVALID", current)
    with engine.connect() as connection:
        assert connection.scalar(select(inference_runs.c.status)) == "FAILED"
    engine.dispose()


def test_prediction_write_keeps_image_uncompleted_until_review(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'accepted.db'}")
    metadata.create_all(engine)
    run_id, project_id, class_id = uuid4(), uuid4(), uuid4()
    generation = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(inference_runs).values(
                id=run_id,
                project_id=project_id,
                training_id=uuid4(),
                model_version=1,
                status="RUNNING",
                targets=[],
                target_image_count=1,
                processed_image_count=0,
                created_box_count=0,
                created_at=generation,
                started_at=generation,
            )
        )
        connection.execute(
            insert(annotation_projects).values(
                id=project_id,
                revision=0,
                updated_at=generation,
            )
        )
        connection.execute(
            insert(annotation_images).values(
                project_id=project_id,
                image_index=0,
                completed=False,
                updated_at=generation,
            )
        )
        connection.execute(
            insert(annotation_classes).values(id=class_id, project_id=project_id)
        )
    rows = _prediction_rows(prediction([0.1, 0.2, 0.5, 0.8]), {0: class_id})
    assert _write_predictions(engine, run_id, project_id, 0, rows, generation) == 1
    with engine.connect() as connection:
        assert connection.scalar(select(annotation_images.c.completed)) is False
        assert connection.scalar(select(annotation_boxes.c.auto_label_run_id)) == run_id
        assert connection.scalar(select(annotation_projects.c.revision)) == 1
    engine.dispose()
