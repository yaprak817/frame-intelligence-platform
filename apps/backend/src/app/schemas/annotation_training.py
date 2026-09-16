from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class StrictTrainingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AnnotationTrainingStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SNAPSHOT_READY = "SNAPSHOT_READY"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AnnotationTrainingConfig(StrictTrainingModel):
    max_snapshot_images: Annotated[StrictInt, Field(ge=50, le=200)] = 200
    epochs: Annotated[StrictInt, Field(ge=1, le=25)] = 10
    batch_size: Annotated[StrictInt, Field(ge=1, le=4)] = 2
    image_size: Annotated[StrictInt, Field(ge=640, le=640)] = 640


class CreateAnnotationTrainingRequest(StrictTrainingModel):
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    config: AnnotationTrainingConfig = Field(default_factory=AnnotationTrainingConfig)


class AnnotationTrainingResponse(StrictTrainingModel):
    id: UUID
    snapshot_version: StrictInt
    source_revision: StrictInt
    status: AnnotationTrainingStatus
    image_count: StrictInt
    class_count: StrictInt
    box_count: StrictInt
    train_image_count: StrictInt
    validation_image_count: StrictInt
    config: AnnotationTrainingConfig
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: StrictStr | None
    progress_completed: StrictInt = 0
    progress_total: StrictInt = 0
    model_version: StrictInt | None = None
    status_url: StrictStr = "/"
    snapshot_download_url: StrictStr | None = None


class LatestAnnotationModelResponse(StrictTrainingModel):
    training_id: UUID
    model_version: StrictInt
    snapshot_version: StrictInt
    created_at: datetime


class AnnotationTrainingPage(StrictTrainingModel):
    items: list[AnnotationTrainingResponse]
    next_cursor: StrictInt | None
    has_more: bool
