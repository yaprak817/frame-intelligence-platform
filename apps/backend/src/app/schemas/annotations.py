import re
import unicodedata
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class StrictAnnotationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def normalized_class_name(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if (
        not normalized
        or len(normalized) > 80
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        raise ValueError("Invalid annotation class name")
    return normalized


class AnnotationLimits(StrictAnnotationModel):
    max_classes: StrictInt
    max_boxes_per_image: StrictInt
    max_boxes_per_project: StrictInt


class AnnotationClassResponse(StrictAnnotationModel):
    id: UUID
    yolo_index: StrictInt
    name: StrictStr
    color: StrictStr


class AnnotationImageSummary(StrictAnnotationModel):
    index: StrictInt
    filename: StrictStr
    completed: StrictBool
    box_count: StrictInt
    preview_url: StrictStr


class AnnotationProjectResponse(StrictAnnotationModel):
    id: UUID
    job_id: UUID
    revision: StrictInt
    classes: list[AnnotationClassResponse]
    images: list[AnnotationImageSummary]
    page: StrictInt
    page_size: StrictInt
    total_images: StrictInt
    limits: AnnotationLimits


class RevisionRequest(StrictAnnotationModel):
    expected_revision: Annotated[StrictInt, Field(ge=0)]


class CreateAnnotationClassRequest(RevisionRequest):
    name: Annotated[StrictStr, Field(min_length=1, max_length=160)]
    color: Annotated[StrictStr, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalized_class_name(value)


class UpdateAnnotationClassRequest(CreateAnnotationClassRequest):
    pass


class AnnotationClassMutationResponse(StrictAnnotationModel):
    revision: StrictInt
    annotation_class: AnnotationClassResponse | None


Coordinate = Annotated[Decimal, Field(ge=0, le=1, max_digits=9, decimal_places=8)]
PositiveCoordinate = Annotated[
    Decimal, Field(gt=0, le=1, max_digits=9, decimal_places=8)
]


class AnnotationBoxInput(StrictAnnotationModel):
    id: UUID
    class_id: UUID
    x_center: Coordinate
    y_center: Coordinate
    width: PositiveCoordinate
    height: PositiveCoordinate

    @model_validator(mode="after")
    def contained(self):
        two = Decimal(2)
        if self.x_center - self.width / two < 0 or self.x_center + self.width / two > 1:
            raise ValueError("Bounding box exceeds horizontal image bounds")
        if (
            self.y_center - self.height / two < 0
            or self.y_center + self.height / two > 1
        ):
            raise ValueError("Bounding box exceeds vertical image bounds")
        return self


class AnnotationBoxResponse(StrictAnnotationModel):
    id: UUID
    class_id: UUID
    x_center: Decimal
    y_center: Decimal
    width: Decimal
    height: Decimal


class PutImageAnnotationsRequest(RevisionRequest):
    completed: StrictBool
    boxes: list[AnnotationBoxInput] = Field(max_length=200)

    @field_validator("boxes")
    @classmethod
    def unique_box_ids(
        cls, value: list[AnnotationBoxInput]
    ) -> list[AnnotationBoxInput]:
        if len({box.id for box in value}) != len(value):
            raise ValueError("Bounding box identifiers must be unique")
        return value


class ImageAnnotationsResponse(StrictAnnotationModel):
    project_revision: StrictInt
    image_index: StrictInt
    completed: StrictBool
    boxes: list[AnnotationBoxResponse]
