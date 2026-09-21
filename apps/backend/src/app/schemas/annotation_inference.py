from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class StrictInferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AnnotationInferenceStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AnnotationInferenceResponse(StrictInferenceModel):
    id: UUID
    training_id: UUID
    model_version: Annotated[StrictInt, Field(ge=1)]
    status: AnnotationInferenceStatus
    target_image_count: Annotated[StrictInt, Field(ge=1)]
    processed_image_count: Annotated[StrictInt, Field(ge=0)]
    created_box_count: Annotated[StrictInt, Field(ge=0)]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: StrictStr | None
    status_url: StrictStr
