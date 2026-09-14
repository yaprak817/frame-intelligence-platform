import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models.annotations import AnnotationBox, AnnotationImage
from app.schemas.annotation_training import (
    AnnotationTrainingConfig,
    CreateAnnotationTrainingRequest,
)
from app.services.annotation_training import AnnotationTrainingService


def image(index: int, digest: str | None = None) -> AnnotationImage:
    return AnnotationImage(
        project_id=uuid4(),
        image_index=index,
        image_filename=f"image-{index}.jpg",
        image_sha256=digest or f"{index:064x}",
        yolo_sha256="a" * 64,
        completed=True,
        updated_at=datetime.now(UTC),
    )


def box(index: int, class_id=None) -> AnnotationBox:
    return AnnotationBox(
        id=uuid4(),
        project_id=uuid4(),
        image_index=index,
        class_id=class_id or uuid4(),
        x_center=Decimal("0.5"),
        y_center=Decimal("0.5"),
        width=Decimal("0.2"),
        height=Decimal("0.2"),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_training_request_is_strict_and_bounded() -> None:
    assert (
        CreateAnnotationTrainingRequest(expected_revision=2).config.max_snapshot_images
        == 200
    )
    for payload in (
        {"expected_revision": "2"},
        {"expected_revision": 2, "extra": True},
        {"expected_revision": 2, "config": {"max_snapshot_images": 49}},
        {"expected_revision": 2, "config": {"max_snapshot_images": 201}},
        {
            "expected_revision": 2,
            "config": {"max_snapshot_images": 50, "extra": True},
        },
    ):
        with pytest.raises(ValidationError):
            CreateAnnotationTrainingRequest.model_validate(payload)


def test_split_is_deterministic_and_has_validation_fallback() -> None:
    project_id = uuid4()
    images = [image(index) for index in range(50)]
    boxes = [box(0)]
    first = AnnotationTrainingService.split(project_id, images, boxes)
    second = AnnotationTrainingService.split(
        project_id, list(reversed(images)), list(reversed(boxes))
    )
    assert first == second
    assert set(first) == set(range(50))
    assert list(first.values()).count("val") == 10
    assert list(first.values()).count("train") == 40


def test_split_keeps_the_only_positive_in_train_and_a_negative_in_validation() -> None:
    project_id = uuid4()
    images = [image(index) for index in range(50)]
    initially_first = min(
        images,
        key=lambda item: (
            hashlib.sha256(
                project_id.bytes
                + item.image_index.to_bytes(8, "big")
                + bytes.fromhex(item.image_sha256)
            ).digest(),
            item.image_index,
        ),
    )
    result = AnnotationTrainingService.split(
        project_id, images, [box(initially_first.image_index)]
    )
    assert result[initially_first.image_index] == "train"
    assert "val" in result.values()
    assert "train" in result.values()


def test_split_gives_each_class_a_train_anchor_and_can_share_an_anchor() -> None:
    project_id = uuid4()
    images = [image(index) for index in range(50)]
    first_class, second_class = uuid4(), uuid4()
    boxes = [box(7, first_class), box(7, second_class), box(8, second_class)]
    result = AnnotationTrainingService.split(project_id, images, boxes)
    assert result[7] == "train"
    for class_id in (first_class, second_class):
        assert any(
            result[item.image_index] == "train"
            for item in boxes
            if item.class_id == class_id
        )
    assert "val" in result.values()


def test_config_rejects_boolean_as_strict_integer() -> None:
    with pytest.raises(ValidationError):
        AnnotationTrainingConfig(max_snapshot_images=True)


def test_project_snapshot_limit_configuration_is_bounded() -> None:
    for value in (0, 21):
        with pytest.raises(ValueError):
            AnnotationTrainingService(None, None, max_snapshots_per_project=value)


def integrity_error(constraint_name: str) -> IntegrityError:
    return IntegrityError(
        "statement",
        {},
        SimpleNamespace(constraint_name=constraint_name),
    )


def test_integrity_recovery_only_accepts_a_known_unique_race_with_winner() -> None:
    project_id = uuid4()
    service = AnnotationTrainingService(SimpleNamespace(), None)
    winner = SimpleNamespace()
    service._idempotent = AsyncMock(return_value=winner)
    recovered = asyncio.run(
        service._recover_unique_race(
            integrity_error("uq_annotation_training_runs_idempotency"),
            project_id=project_id,
            idempotency_key="snapshot-request-1",
            fingerprint="a" * 64,
            source_revision=3,
        )
    )
    assert recovered is winner

    service._idempotent = AsyncMock(return_value=None)
    missing_winner = integrity_error("uq_annotation_training_runs_idempotency")
    with pytest.raises(IntegrityError) as raised:
        asyncio.run(
            service._recover_unique_race(
                missing_winner,
                project_id=project_id,
                idempotency_key="snapshot-request-1",
                fingerprint="a" * 64,
                source_revision=3,
            )
        )
    assert raised.value is missing_winner


@pytest.mark.parametrize(
    "constraint_name",
    ["fk_snapshot_box_image", "ck_snapshot_box_width", "unknown_constraint"],
)
def test_non_unique_integrity_errors_are_not_reclassified(constraint_name: str) -> None:
    service = AnnotationTrainingService(SimpleNamespace(), None)
    service._idempotent = AsyncMock()
    error = integrity_error(constraint_name)
    with pytest.raises(IntegrityError) as raised:
        asyncio.run(
            service._recover_unique_race(
                error,
                project_id=uuid4(),
                idempotency_key="snapshot-request-1",
                fingerprint="a" * 64,
                source_revision=3,
            )
        )
    assert raised.value is error
    service._idempotent.assert_not_awaited()
