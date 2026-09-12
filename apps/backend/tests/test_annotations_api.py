from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_annotation_service
from app.main import app
from app.models.annotations import AnnotationProject
from app.schemas.annotations import (
    AnnotationBoxResponse,
    AnnotationImageSummary,
    AnnotationLimits,
    AnnotationProjectResponse,
    ImageAnnotationsResponse,
)
from app.services.annotations import AnnotationClassInvalid, AnnotationRevisionConflict
from app.services.result_artifacts import ManifestInvalidError
from app.storage.s3 import ObjectMetadata, ObjectStream


class FakeAnnotationService:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.project = AnnotationProject(
            id=uuid4(),
            job_id=uuid4(),
            result_run_token=uuid4(),
            revision=0,
            created_at=now,
            updated_at=now,
        )
        self.put_error = AnnotationRevisionConflict()
        self.preview_error = None

    async def get_or_create(self, job_id):
        assert job_id == self.project.job_id
        return self.project, True

    async def get(self, job_id):
        assert job_id == self.project.job_id
        return self.project

    async def response(self, project, page, page_size):
        return AnnotationProjectResponse(
            id=project.id,
            job_id=project.job_id,
            revision=project.revision,
            classes=[],
            images=[
                AnnotationImageSummary(
                    index=0,
                    filename="safe.jpg",
                    completed=False,
                    box_count=0,
                    preview_url=(
                        f"/api/v1/jobs/{project.job_id}/annotations/images/0/preview"
                    ),
                )
            ],
            page=page,
            page_size=page_size,
            total_images=1,
            limits=AnnotationLimits(
                max_classes=100,
                max_boxes_per_image=200,
                max_boxes_per_project=50_000,
            ),
        )

    async def put_image(self, project, image_index, request):
        if self.put_error is not None:
            raise self.put_error
        project.revision += 1
        return ImageAnnotationsResponse(
            project_revision=project.revision,
            image_index=image_index,
            completed=request.completed,
            boxes=[
                AnnotationBoxResponse(
                    id=box.id,
                    class_id=box.class_id,
                    x_center=box.x_center,
                    y_center=box.y_center,
                    width=box.width,
                    height=box.height,
                )
                for box in request.boxes
            ],
        )

    async def image(self, project, image_index):
        assert image_index == 0
        return ImageAnnotationsResponse(
            project_revision=project.revision,
            image_index=image_index,
            completed=True,
            boxes=[
                AnnotationBoxResponse(
                    id=uuid4(),
                    class_id=uuid4(),
                    x_center=Decimal("0.5"),
                    y_center=Decimal("5e-1"),
                    width=Decimal("0.25"),
                    height=Decimal("2.5e-1"),
                )
            ],
        )

    async def preview(self, project, image_index):
        assert image_index == 0
        if self.preview_error is not None:
            raise self.preview_error
        return ObjectStream(
            body=BytesIO(b"jpeg"),
            metadata=ObjectMetadata(4, "image/jpeg", "a" * 64),
        )


@pytest.fixture
def annotation_client():
    service = FakeAnnotationService()
    app.dependency_overrides[get_annotation_service] = lambda: service
    with TestClient(app) as client:

        class AllowingLimiter:
            async def check(self, **_kwargs):
                from app.security.rate_limit import RateLimitDecision

                return RateLimitDecision(True, 1)

        client.app.state.rate_limiter = AllowingLimiter()
        yield client, service
    app.dependency_overrides.clear()


def test_project_response_is_paginated_and_public_safe(annotation_client) -> None:
    client, service = annotation_client
    response = client.post(
        f"/api/v1/jobs/{service.project.job_id}/annotations?page=1&page_size=10"
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["images"][0]["preview_url"].startswith("/api/v1/")
    for secret in ("result_run_token", "object_key", "bucket", "s3://"):
        assert secret not in response.text


def test_strict_put_and_conflict_are_safe(annotation_client) -> None:
    client, service = annotation_client
    path = f"/api/v1/jobs/{service.project.job_id}/annotations/images/0"
    invalid = client.put(
        path, json={"expected_revision": 0, "completed": 1, "boxes": []}
    )
    conflict = client.put(
        path, json={"expected_revision": 0, "completed": True, "boxes": []}
    )
    assert invalid.status_code == 422
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ANNOTATION_REVISION_CONFLICT"


def test_non_empty_frontend_json_box_is_bound_and_returned(annotation_client) -> None:
    client, service = annotation_client
    service.put_error = None
    box_id, class_id = uuid4(), uuid4()
    response = client.put(
        f"/api/v1/jobs/{service.project.job_id}/annotations/images/0",
        json={
            "expected_revision": 0,
            "completed": True,
            "boxes": [
                {
                    "id": str(box_id),
                    "class_id": str(class_id),
                    "x_center": 0.5,
                    "y_center": 0.5,
                    "width": 0.2,
                    "height": 0.2,
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "project_revision": 1,
        "image_index": 0,
        "completed": True,
        "boxes": [
            {
                "id": str(box_id),
                "class_id": str(class_id),
                "x_center": "0.5",
                "y_center": "0.5",
                "width": "0.2",
                "height": "0.2",
            }
        ],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "11111111-1111-4111-8111-11111111111Z"),
        ("id", "11111111-1111-4111-8111-111111111111 "),
        ("x_center", True),
        ("x_center", " 0.5"),
        ("x_center", "NaN"),
        ("x_center", "Infinity"),
        ("x_center", "0x1"),
        ("x_center", "0.5tail"),
        ("x_center", "1" * 65),
        ("width", 0),
    ],
)
def test_invalid_frontend_box_values_are_safe_422(
    annotation_client, field, value
) -> None:
    client, service = annotation_client
    service.put_error = None
    box = {
        "id": str(uuid4()),
        "class_id": str(uuid4()),
        "x_center": 0.5,
        "y_center": 0.5,
        "width": 0.2,
        "height": 0.2,
    }
    box[field] = value
    response = client.put(
        f"/api/v1/jobs/{service.project.job_id}/annotations/images/0",
        json={"expected_revision": 0, "completed": True, "boxes": [box]},
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
        }
    }
    assert str(value) not in response.text


def test_non_empty_box_response_uses_public_decimal_json_contract(
    annotation_client,
) -> None:
    client, service = annotation_client
    response = client.get(f"/api/v1/jobs/{service.project.job_id}/annotations/images/0")
    assert response.status_code == 200
    box = response.json()["boxes"][0]
    assert {key: box[key] for key in ("x_center", "y_center", "width", "height")} == {
        "x_center": "0.5",
        "y_center": "0.5",
        "width": "0.25",
        "height": "0.25",
    }
    assert not any(
        secret in response.text
        for secret in ("result_run_token", "object_key", "bucket", "s3://")
    )


def test_preview_is_same_origin_stream_with_safe_headers(annotation_client) -> None:
    client, service = annotation_client
    response = client.get(
        f"/api/v1/jobs/{service.project.job_id}/annotations/images/0/preview"
    )
    assert response.status_code == 200
    assert response.content == b"jpeg"
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "s3://" not in response.text


def test_invalid_preview_fails_before_streaming_without_internal_details(
    annotation_client,
) -> None:
    client, service = annotation_client
    service.preview_error = ManifestInvalidError(
        "bucket=private key=secret internal=http://minio:9000 path=C:/spool"
    )
    response = client.get(
        f"/api/v1/jobs/{service.project.job_id}/annotations/images/0/preview"
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ANNOTATION_SOURCE_CHANGED"
    assert not any(
        value in response.text.lower()
        for value in ("bucket", "key=", "minio", "spool", "c:/")
    )


def test_annotation_payload_is_bounded_before_json_parsing(annotation_client) -> None:
    client, service = annotation_client
    response = client.put(
        f"/api/v1/jobs/{service.project.job_id}/annotations/images/0",
        content=b"x" * (256 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "ANNOTATION_LIMIT_EXCEEDED"


def test_constraint_failure_response_does_not_leak_database_details(
    annotation_client,
) -> None:
    client, service = annotation_client
    service.put_error = AnnotationClassInvalid(
        'duplicate key violates unique constraint "annotation_boxes_pkey"'
    )
    response = client.put(
        f"/api/v1/jobs/{service.project.job_id}/annotations/images/0",
        json={"expected_revision": 0, "completed": True, "boxes": []},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ANNOTATION_CLASS_INVALID"
    assert not any(
        secret in response.text.lower()
        for secret in ("duplicate key", "constraint", "annotation_boxes", "sql")
    )
