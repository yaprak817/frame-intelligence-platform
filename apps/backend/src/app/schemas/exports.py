from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ExportStatus(StrEnum):
    PREPARING = "PREPARING"
    READY = "READY"
    FAILED = "FAILED"


class CreateFrameExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["all", "selected"]
    frame_indices: list[Annotated[StrictInt, Field(ge=0)]] | None = Field(
        default=None, max_length=1000
    )

    @model_validator(mode="after")
    def validate_selection(self):
        if self.mode == "all" and self.frame_indices is not None:
            raise ValueError("all mode must omit frame_indices")
        if self.mode == "selected" and not self.frame_indices:
            raise ValueError("selected mode requires frame_indices")
        if self.frame_indices and len(set(self.frame_indices)) != len(
            self.frame_indices
        ):
            raise ValueError("frame_indices must be unique")
        return self


class FrameExportResponse(BaseModel):
    id: UUID
    job_id: UUID
    status: ExportStatus
    mode: Literal["all", "selected"]
    frame_count: int
    created_at: datetime
    completed_at: datetime | None
    status_url: str
    download_url: str | None
    failure_code: str | None
