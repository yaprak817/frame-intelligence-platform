from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_annotation_training_service
from app.main import app
from app.schemas.annotation_training import (
    AnnotationTrainingConfig,
    AnnotationTrainingPage,
    AnnotationTrainingResponse,
    AnnotationTrainingStatus,
)
from app.services.annotation_training import AnnotationTrainingNotFound


class FakeTrainingService:
    def __init__(self) -> None:
        self.job_id = uuid4()
        self.item = AnnotationTrainingResponse(
            id=uuid4(),
            snapshot_version=1,
            source_revision=7,
            status=AnnotationTrainingStatus.SNAPSHOT_READY,
            image_count=50,
            class_count=2,
            box_count=8,
            train_image_count=40,
            validation_image_count=10,
            config=AnnotationTrainingConfig(max_snapshot_images=200),
            created_at=datetime.now(UTC),
            started_at=None,
            completed_at=datetime.now(UTC),
            failure_code=None,
        )
        self.created = True
        self.calls = 0
        self.error = None

    async def create(self, job_id, request, key):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert job_id == self.job_id
        assert request.expected_revision == 7
        assert key == "training-request-0001"
        return self.item, self.created

    async def list_runs(self, job_id, *, limit, after_snapshot_version):
        assert job_id == self.job_id
        assert limit in {1, 20}
        assert after_snapshot_version in {None, 1}
        return AnnotationTrainingPage(
            items=[] if after_snapshot_version == 1 else [self.item],
            next_cursor=1 if limit == 1 and after_snapshot_version is None else None,
            has_more=limit == 1 and after_snapshot_version is None,
        )

    async def get(self, job_id, training_id):
        if job_id != self.job_id or training_id != self.item.id:
            raise AnnotationTrainingNotFound
        return self.item

    async def start(self, job_id, training_id):
        if job_id != self.job_id or training_id != self.item.id:
            raise AnnotationTrainingNotFound
        created = self.item.status == AnnotationTrainingStatus.SNAPSHOT_READY
        self.item = self.item.model_copy(
            update={
                "status": AnnotationTrainingStatus.PENDING,
                "progress_total": self.item.config.epochs,
                "completed_at": None,
            }
        )
        return self.item, created


@pytest.fixture
def training_client():
    service = FakeTrainingService()
    app.dependency_overrides[get_annotation_training_service] = lambda: service
    with TestClient(app) as client:

        class AllowingLimiter:
            async def check(self, **_kwargs):
                from app.security.rate_limit import RateLimitDecision

                return RateLimitDecision(True, 1)

        client.app.state.rate_limiter = AllowingLimiter()
        yield client, service
    app.dependency_overrides.clear()


def test_create_list_and_get_training_are_public_safe(training_client) -> None:
    client, service = training_client
    path = f"/api/v1/jobs/{service.job_id}/annotations/trainings"
    created = client.post(
        path,
        headers={"Idempotency-Key": "training-request-0001"},
        json={"expected_revision": 7, "config": {"max_snapshot_images": 200}},
    )
    listed = client.get(path)
    fetched = client.get(f"{path}/{service.item.id}")
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert listed.status_code == fetched.status_code == 200
    assert listed.json() == {
        "items": [created.json()],
        "next_cursor": None,
        "has_more": False,
    }
    assert fetched.json() == created.json()
    assert set(created.json()) == {
        "id",
        "snapshot_version",
        "source_revision",
        "status",
        "image_count",
        "class_count",
        "box_count",
        "train_image_count",
        "validation_image_count",
        "config",
        "created_at",
        "started_at",
        "completed_at",
        "failure_code",
        "progress_completed",
        "progress_total",
        "model_version",
        "status_url",
        "snapshot_download_url",
    }
    for secret in (
        "result_run_token",
        "bucket",
        "object_key",
        "s3://",
        "outbox",
        "traceback",
    ):
        assert secret not in created.text.lower()


def test_training_request_rejects_missing_idempotency_and_coercion(
    training_client,
) -> None:
    client, service = training_client
    path = f"/api/v1/jobs/{service.job_id}/annotations/trainings"
    missing = client.post(path, json={"expected_revision": 7})
    invalid = client.post(
        path,
        headers={"Idempotency-Key": "training-request-0001"},
        json={"expected_revision": "7", "unexpected": True},
    )
    assert missing.status_code == invalid.status_code == 422


def test_foreign_training_is_fail_closed(training_client) -> None:
    client, service = training_client
    response = client.get(
        f"/api/v1/jobs/{service.job_id}/annotations/trainings/{uuid4()}"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ANNOTATION_TRAINING_NOT_FOUND"


def test_training_list_has_bounded_cursor_contract(training_client) -> None:
    client, service = training_client
    path = f"/api/v1/jobs/{service.job_id}/annotations/trainings"
    first = client.get(path, params={"limit": "1"})
    last = client.get(path, params={"limit": "1", "after_snapshot_version": "1"})
    assert first.status_code == last.status_code == 200
    assert first.json()["has_more"] is True
    assert first.json()["next_cursor"] == 1
    assert last.json() == {"items": [], "next_cursor": None, "has_more": False}
    for params in (
        {"limit": "0"},
        {"limit": "51"},
        {"limit": "true"},
        {"after_snapshot_version": "-1"},
        {"after_snapshot_version": "false"},
    ):
        assert client.get(path, params=params).status_code == 422


def test_training_paths_require_canonical_uuids_before_service(training_client) -> None:
    client, service = training_client
    canonical_job = str(service.job_id)
    canonical_training = str(service.item.id)
    invalid_jobs = (
        canonical_job.upper(),
        canonical_job.replace("-", ""),
        "{" + canonical_job + "}",
        "urn:uuid:" + canonical_job,
    )
    for raw_job in invalid_jobs:
        response = client.post(
            f"/api/v1/jobs/{raw_job}/annotations/trainings",
            headers={"Idempotency-Key": "training-request-0001"},
            json={"expected_revision": 7},
        )
        assert response.status_code == 422
    for raw_training in (
        canonical_training.upper(),
        canonical_training.replace("-", ""),
        "{" + canonical_training + "}",
        "urn:uuid:" + canonical_training,
    ):
        assert (
            client.get(
                f"/api/v1/jobs/{canonical_job}/annotations/trainings/{raw_training}"
            ).status_code
            == 422
        )
    assert service.calls == 0


def test_unexpected_training_error_is_safe_503(training_client) -> None:
    client, service = training_client
    service.error = RuntimeError(
        "violates uq_annotation_training_runs_source_revision SQL secret"
    )
    response = client.post(
        f"/api/v1/jobs/{service.job_id}/annotations/trainings",
        headers={"Idempotency-Key": "training-request-0001"},
        json={"expected_revision": 7},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "ANNOTATION_TRAINING_UNAVAILABLE",
        "message": "Annotation training service is unavailable",
    }
    assert "sql" not in response.text.lower()
    assert "constraint" not in response.text.lower()


def test_start_training_is_idempotent_and_foreign_safe(training_client) -> None:
    client, service = training_client
    path = (
        f"/api/v1/jobs/{service.job_id}/annotations/trainings/{service.item.id}/start"
    )
    first = client.post(path)
    duplicate = client.post(path)
    foreign = client.post(path.replace(str(service.item.id), str(uuid4())))
    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert first.json()["status"] == duplicate.json()["status"] == "PENDING"
    assert foreign.status_code == 404
