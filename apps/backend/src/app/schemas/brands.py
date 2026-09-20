from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def canonical_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("Canonical UUID required")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("Canonical UUID required")
    return parsed


CanonicalUUID = Annotated[UUID, BeforeValidator(canonical_uuid)]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrandCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=120)


class BrandClassCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=80)
    color: str = Field(default="#7C3AED", pattern=r"^#[0-9A-Fa-f]{6}$")


class BrandDatasetAttach(StrictRequest):
    name: str = Field(min_length=1, max_length=160)
    job_id: CanonicalUUID | None = None
    annotation_project_id: CanonicalUUID | None = None


class BrandClassView(BaseModel):
    id: UUID
    name: str
    color: str


class BrandDatasetView(BaseModel):
    id: UUID
    name: str
    job_id: UUID | None
    annotation_project_id: UUID | None


class BrandSummary(BaseModel):
    id: UUID
    name: str
    status: str
    class_count: int
    dataset_count: int
    updated_at: datetime


class BrandDetail(BaseModel):
    id: UUID
    name: str
    status: str
    created_at: datetime
    updated_at: datetime
    classes: list[BrandClassView]
    datasets: list[BrandDatasetView]
