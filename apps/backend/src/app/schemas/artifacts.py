import re
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FRAME_FILENAME_PATTERN = re.compile(
    r"^frame_(?P<index>\d{6})_(?P<timestamp>\d+)ms_"
    r"(?P<width>\d+)x(?P<height>\d+)\.jpg$"
)

PositiveStrictInt = Annotated[StrictInt, Field(gt=0)]
NonNegativeStrictInt = Annotated[StrictInt, Field(ge=0)]
NonNegativeNumber = Annotated[float, Field(ge=0)]


class StrictArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ManifestSummaryV1(StrictArtifactModel):
    frames_saved: NonNegativeStrictInt
    candidates: NonNegativeStrictInt
    shortlisted: NonNegativeStrictInt
    duplicates_removed: NonNegativeStrictInt
    processing_seconds: NonNegativeNumber
    duration_seconds: NonNegativeNumber


class ManifestFrameV1(StrictArtifactModel):
    index: NonNegativeStrictInt
    filename: StrictStr
    object_key: StrictStr
    content_type: StrictStr
    size_bytes: PositiveStrictInt
    sha256: StrictStr
    timestamp_ms: NonNegativeStrictInt
    width: PositiveStrictInt
    height: PositiveStrictInt

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("Invalid SHA-256")
        return value


class StoredManifestV1(StrictArtifactModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    job_id: UUID
    run_token: UUID
    created_at: datetime
    summary: ManifestSummaryV1
    frames: list[ManifestFrameV1] = Field(max_length=10_000)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC")
        return value


class PublicFrame(BaseModel):
    index: int
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    timestamp_ms: int
    width: int
    height: int
    access_url: str


class PublicResultManifest(BaseModel):
    schema_version: int
    job_id: UUID
    created_at: datetime
    summary: ManifestSummaryV1
    frames: list[PublicFrame]


class FrameAccessResponse(BaseModel):
    url: str
    expires_at: datetime
    content_type: str
    size_bytes: int
    sha256: str
