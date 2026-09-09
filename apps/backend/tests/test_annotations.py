import asyncio
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.jobs import JobStatus, SourceType
from app.schemas.annotations import (
    AnnotationBoxResponse,
    AnnotationClassMutationResponse,
    AnnotationClassResponse,
    AnnotationImageSummary,
    AnnotationLimits,
    AnnotationProjectResponse,
    CreateAnnotationClassRequest,
    ImageAnnotationsResponse,
    PutImageAnnotationsRequest,
)
from app.security.rate_limit import protected_group
from app.services.annotations import AnnotationNotAvailable, AnnotationService


def test_class_request_is_strict_normalized_and_bounded() -> None:
    value = CreateAnnotationClassRequest.model_validate(
        {"expected_revision": 0, "name": "  U\u0308lker  ", "color": "#aBc123"}
    )
    assert value.name == "Ülker"
    with pytest.raises(ValidationError):
        CreateAnnotationClassRequest.model_validate(
            {"expected_revision": 0, "name": "bad\nname", "color": "#abcdef"}
        )
    with pytest.raises(ValidationError):
        CreateAnnotationClassRequest.model_validate(
            {"expected_revision": True, "name": "Avis", "color": "red"}
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("x_center", "NaN"),
        ("y_center", "Infinity"),
        ("width", 0),
        ("height", -1),
        ("x_center", Decimal("0.91")),
    ],
)
def test_box_coordinates_are_finite_positive_and_contained(field, value) -> None:
    box = {
        "id": uuid4(),
        "class_id": uuid4(),
        "x_center": Decimal("0.5"),
        "y_center": Decimal("0.5"),
        "width": Decimal("0.2"),
        "height": Decimal("0.2"),
    }
    box[field] = value
    with pytest.raises(ValidationError):
        PutImageAnnotationsRequest.model_validate(
            {"expected_revision": 0, "completed": False, "boxes": [box]}
        )


def test_duplicate_box_identifiers_and_extra_fields_are_rejected() -> None:
    identifier = uuid4()
    box = {
        "id": identifier,
        "class_id": uuid4(),
        "x_center": Decimal("0.5"),
        "y_center": Decimal("0.5"),
        "width": Decimal("0.2"),
        "height": Decimal("0.2"),
    }
    with pytest.raises(ValidationError):
        PutImageAnnotationsRequest.model_validate(
            {
                "expected_revision": 0,
                "completed": False,
                "boxes": [box, box],
            }
        )
    with pytest.raises(ValidationError):
        PutImageAnnotationsRequest.model_validate(
            {"expected_revision": 0, "completed": 0, "boxes": [], "pixels": []}
        )


@pytest.mark.parametrize(
    ("method", "suffix", "expected"),
    [
        ("GET", "", ("annotation-read", "rate_limit_annotation_read_requests")),
        (
            "GET",
            "/images/0/preview",
            ("annotation-read", "rate_limit_annotation_read_requests"),
        ),
        (
            "POST",
            "/classes",
            ("annotation-mutation", "rate_limit_annotation_mutation_requests"),
        ),
        (
            "PUT",
            "/images/0",
            ("annotation-mutation", "rate_limit_annotation_mutation_requests"),
        ),
    ],
)
def test_annotation_rate_limits_are_separate(method, suffix, expected) -> None:
    path = f"/api/v1/jobs/11111111-1111-4111-8111-111111111111/annotations{suffix}"
    assert protected_group(method, path) == expected


def _valid_public_responses():
    class_id = uuid4()
    box_id = uuid4()
    job_id = uuid4()
    project_id = uuid4()
    return [
        (
            AnnotationLimits,
            {
                "max_classes": 100,
                "max_boxes_per_image": 200,
                "max_boxes_per_project": 50_000,
            },
        ),
        (
            AnnotationClassResponse,
            {"id": class_id, "yolo_index": 0, "name": "car", "color": "#ABC123"},
        ),
        (
            AnnotationImageSummary,
            {
                "index": 0,
                "filename": "safe.jpg",
                "completed": False,
                "box_count": 0,
                "preview_url": "/api/v1/preview",
            },
        ),
        (
            AnnotationProjectResponse,
            {
                "id": project_id,
                "job_id": job_id,
                "revision": 0,
                "classes": [],
                "images": [],
                "page": 1,
                "page_size": 50,
                "total_images": 0,
                "limits": {
                    "max_classes": 100,
                    "max_boxes_per_image": 200,
                    "max_boxes_per_project": 50_000,
                },
            },
        ),
        (AnnotationClassMutationResponse, {"revision": 1, "annotation_class": None}),
        (
            AnnotationBoxResponse,
            {
                "id": box_id,
                "class_id": class_id,
                "x_center": Decimal("0.5"),
                "y_center": Decimal("0.5"),
                "width": Decimal("0.2"),
                "height": Decimal("0.2"),
            },
        ),
        (
            ImageAnnotationsResponse,
            {"project_revision": 1, "image_index": 0, "completed": True, "boxes": []},
        ),
    ]


@pytest.mark.parametrize(
    "internal_field",
    [
        "result_run_token",
        "object_key",
        "yolo_object_key",
        "bucket",
        "storage_reference",
        "internal_url",
        "lease_token",
    ],
)
def test_public_response_models_reject_internal_fields(internal_field) -> None:
    for model, payload in _valid_public_responses():
        with pytest.raises(ValidationError):
            model.model_validate({**payload, internal_field: "must-not-leak"})


@pytest.mark.parametrize(
    ("model", "payload", "field", "invalid"),
    [
        (
            AnnotationLimits,
            {
                "max_classes": 100,
                "max_boxes_per_image": 200,
                "max_boxes_per_project": 50_000,
            },
            "max_classes",
            "100",
        ),
        (
            AnnotationClassResponse,
            {"id": uuid4(), "yolo_index": 0, "name": "car", "color": "#ABC123"},
            "yolo_index",
            1.0,
        ),
        (
            AnnotationClassResponse,
            {"id": uuid4(), "yolo_index": 0, "name": "car", "color": "#ABC123"},
            "name",
            123,
        ),
        (
            AnnotationImageSummary,
            {
                "index": 0,
                "filename": "safe.jpg",
                "completed": False,
                "box_count": 0,
                "preview_url": "/preview",
            },
            "completed",
            1,
        ),
        (
            ImageAnnotationsResponse,
            {"project_revision": 1, "image_index": 0, "completed": True, "boxes": []},
            "image_index",
            "0",
        ),
    ],
)
def test_public_response_models_reject_primitive_coercion(
    model, payload, field, invalid
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, field: invalid})


def test_public_response_models_accept_valid_api_values() -> None:
    for model, payload in _valid_public_responses():
        assert model.model_validate(payload).model_dump() == payload


@pytest.mark.parametrize(
    ("status", "source_type"),
    [
        (JobStatus.PENDING_DISPATCH, SourceType.IMAGE_DATASET),
        (JobStatus.QUEUED, SourceType.IMAGE_DATASET),
        (JobStatus.RUNNING, SourceType.IMAGE_DATASET),
        (JobStatus.FAILED, SourceType.IMAGE_DATASET),
        (JobStatus.SUCCEEDED, SourceType.URL),
        (JobStatus.SUCCEEDED, SourceType.UPLOAD),
    ],
)
def test_annotation_project_creation_rejects_ineligible_jobs_before_db_mutation(
    status, source_type
) -> None:
    class Results:
        async def annotation_source(self, job_id):
            return (
                SimpleNamespace(id=job_id, status=status, source_type=source_type),
                SimpleNamespace(run_token=uuid4()),
            )

    class NoDatabaseAccess:
        def __getattr__(self, name):
            raise AssertionError(f"database mutation attempted through {name}")

    with pytest.raises(AnnotationNotAvailable):
        asyncio.run(
            AnnotationService(NoDatabaseAccess(), Results()).get_or_create(uuid4())
        )
