import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_annotation_inference_service
from app.main import app
from app.outbox.celery_client import CeleryJobMessagePublisher
from app.schemas.annotation_inference import (
    AnnotationInferenceResponse,
    AnnotationInferenceStatus,
)
from app.services.annotation_inference import (
    AnnotationInferenceLimitExceeded,
    AnnotationInferenceModelNotFound,
    AnnotationInferenceNotFound,
    AnnotationInferenceService,
)


class FakeInferenceService:
    def __init__(self) -> None:
        self.job_id = uuid4()
        self.item = AnnotationInferenceResponse(
            id=uuid4(),
            training_id=uuid4(),
            model_version=2,
            status=AnnotationInferenceStatus.PENDING,
            target_image_count=12,
            processed_image_count=0,
            created_box_count=0,
            created_at=datetime.now(UTC),
            started_at=None,
            completed_at=None,
            failure_code=None,
            status_url="/",
        )
        self.item = self.item.model_copy(
            update={
                "status_url": (
                    f"/api/v1/jobs/{self.job_id}/annotations/auto-label/{self.item.id}"
                )
            }
        )
        self.created = True
        self.error: Exception | None = None
        self.calls = 0

    async def start(self, job_id):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert job_id == self.job_id
        return self.item, self.created

    async def get(self, job_id, run_id):
        if job_id != self.job_id or run_id != self.item.id:
            raise AnnotationInferenceNotFound
        return self.item

    async def latest(self, job_id):
        if job_id != self.job_id:
            raise AnnotationInferenceNotFound
        return self.item


@pytest.fixture
def inference_client():
    service = FakeInferenceService()
    app.dependency_overrides[get_annotation_inference_service] = lambda: service
    with TestClient(app) as client:

        class AllowingLimiter:
            async def check(self, **_kwargs):
                from app.security.rate_limit import RateLimitDecision

                return RateLimitDecision(True, 1)

        client.app.state.rate_limiter = AllowingLimiter()
        yield client, service
    app.dependency_overrides.clear()


def test_start_get_and_latest_inference_are_public_safe(inference_client) -> None:
    client, service = inference_client
    path = f"/api/v1/jobs/{service.job_id}/annotations/auto-label"
    started = client.post(path)
    fetched = client.get(f"{path}/{service.item.id}")
    latest = client.get(f"{path}/latest")
    assert started.status_code == 202
    assert started.headers["cache-control"] == "no-store"
    assert fetched.status_code == latest.status_code == 200
    assert fetched.json() == latest.json() == started.json()
    assert set(started.json()) == {
        "id",
        "training_id",
        "model_version",
        "status",
        "target_image_count",
        "processed_image_count",
        "created_box_count",
        "created_at",
        "started_at",
        "completed_at",
        "failure_code",
        "status_url",
    }
    for secret in ("bucket", "object_key", "s3://", "targets", "traceback"):
        assert secret not in started.text.lower()


def test_duplicate_start_returns_existing_run(inference_client) -> None:
    client, service = inference_client
    service.created = False
    response = client.post(f"/api/v1/jobs/{service.job_id}/annotations/auto-label")
    assert response.status_code == 200
    assert response.json()["id"] == str(service.item.id)


def test_missing_model_is_a_safe_conflict(inference_client) -> None:
    client, service = inference_client
    service.error = AnnotationInferenceModelNotFound()
    response = client.post(f"/api/v1/jobs/{service.job_id}/annotations/auto-label")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ANNOTATION_INFERENCE_MODEL_NOT_FOUND"


def test_too_many_targets_is_a_safe_conflict(inference_client) -> None:
    client, service = inference_client
    service.error = AnnotationInferenceLimitExceeded()
    response = client.post(f"/api/v1/jobs/{service.job_id}/annotations/auto-label")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ANNOTATION_INFERENCE_LIMIT_EXCEEDED"


def test_inference_paths_require_canonical_uuids_before_service(
    inference_client,
) -> None:
    client, service = inference_client
    canonical_job = str(service.job_id)
    canonical_run = str(service.item.id)
    for raw_job in (
        canonical_job.upper(),
        canonical_job.replace("-", ""),
        "{" + canonical_job + "}",
    ):
        response = client.post(f"/api/v1/jobs/{raw_job}/annotations/auto-label")
        assert response.status_code == 422
    for raw_run in (
        canonical_run.upper(),
        canonical_run.replace("-", ""),
        "{" + canonical_run + "}",
    ):
        assert (
            client.get(
                f"/api/v1/jobs/{canonical_job}/annotations/auto-label/{raw_run}"
            ).status_code
            == 422
        )
    assert service.calls == 0


def test_inference_dispatch_uses_ml_queue_and_canonical_task_name(monkeypatch) -> None:
    publisher = CeleryJobMessagePublisher("memory://", ml_task_queue="annotation-ml")
    calls = []
    monkeypatch.setattr(
        publisher._app,
        "send_task",
        lambda name, **kwargs: calls.append((name, kwargs)),
    )
    inference_id = uuid4()
    try:
        publisher.publish_inference(inference_id)
    finally:
        publisher.close()
    assert calls == [
        (
            "frame_worker.auto_label_annotations",
            {"kwargs": {"inference_id": str(inference_id)}, "queue": "annotation-ml"},
        )
    ]


def test_successful_model_creates_one_unlabeled_run_and_dispatches() -> None:
    job_id, project_id, training_id, class_id, run_token = (uuid4() for _ in range(5))
    project = SimpleNamespace(
        id=project_id, job_id=job_id, result_run_token=run_token, brand_id=None
    )
    training = SimpleNamespace(id=training_id, model_version=2)
    image = SimpleNamespace(image_index=0, image_sha256="a" * 64, yolo_sha256="b" * 64)
    frame = SimpleNamespace(
        index=0,
        object_key="jobs/image.jpg",
        size_bytes=3,
        content_type="image/jpeg",
        filename="image.jpg",
        sha256="a" * 64,
        width=2,
        height=2,
    )
    manifest = SimpleNamespace(run_token=run_token, frames=[frame])

    class Results:
        async def annotation_source(self, requested_job):
            assert requested_job == job_id
            return None, manifest

    class Rows:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class Session:
        def __init__(self):
            self.scalar_results = iter([project, None, training])
            self.scalars_results = iter(
                [
                    [SimpleNamespace(class_id=class_id, yolo_index=0)],
                    [class_id],
                    [image],
                ]
            )
            self.added = []
            self.commits = 0

        async def scalar(self, _query):
            return next(self.scalar_results)

        async def scalars(self, _query):
            return Rows(next(self.scalars_results))

        def add(self, run):
            self.added.append(run)

        async def flush(self):
            pass

        async def commit(self):
            self.commits += 1

    session = Session()
    published = []
    service = AnnotationInferenceService(session, Results())
    response, created = asyncio.run(service.start(job_id))
    assert created and response.status == AnnotationInferenceStatus.PENDING
    assert session.commits == 1 and published == []
    assert session.added[1].inference_id == response.id
    assert session.added[0].targets == [
        {
            "image_index": 0,
            "source_object_key": "jobs/image.jpg",
            "source_size_bytes": 3,
            "source_content_type": "image/jpeg",
            "source_sha256": "a" * 64,
            "width": 2,
            "height": 2,
        }
    ]
    for internal in ("object_key", "bucket", "run_token", "secret", "traceback"):
        assert internal not in response.model_dump_json()
