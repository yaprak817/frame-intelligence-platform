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


class DatasetSummaryV1(StrictArtifactModel):
    uploaded_files: NonNegativeStrictInt
    accepted_files: NonNegativeStrictInt
    normal: NonNegativeStrictInt
    challenging: NonNegativeStrictInt
    unusable: NonNegativeStrictInt
    rejected: NonNegativeStrictInt
    duplicates: NonNegativeStrictInt
    ignored_metadata_entries: NonNegativeStrictInt
    recommended_count: NonNegativeStrictInt
    recommended_normal: NonNegativeStrictInt
    recommended_challenging: NonNegativeStrictInt
    target_challenging_ratio: Annotated[float, Field(ge=0, le=1)]
    actual_challenging_ratio: Annotated[float, Field(ge=0, le=1)]
    ratio_note: str | None


class DatasetPadding(StrictArtifactModel):
    top: NonNegativeStrictInt
    right: NonNegativeStrictInt
    bottom: NonNegativeStrictInt
    left: NonNegativeStrictInt


class StoredDatasetImageV1(StrictArtifactModel):
    index: NonNegativeStrictInt
    filename: StrictStr
    content_type: StrictStr
    size_bytes: NonNegativeStrictInt
    sha256: StrictStr
    width: NonNegativeStrictInt
    height: NonNegativeStrictInt
    quality_category: str
    sharpness: NonNegativeNumber
    brightness: NonNegativeNumber
    underexposed_ratio: Annotated[float, Field(ge=0, le=1)]
    overexposed_ratio: Annotated[float, Field(ge=0, le=1)]
    resolution_usable: bool
    quality_score: Annotated[float, Field(ge=0, le=1)]
    duplicate: bool
    object_key: str | None
    yolo_object_key: str | None
    yolo_size_bytes: NonNegativeStrictInt
    yolo_sha256: StrictStr
    output_width: NonNegativeStrictInt
    output_height: NonNegativeStrictInt
    resize_scale: NonNegativeNumber
    padding: DatasetPadding

    @field_validator("sha256")
    @classmethod
    def dataset_sha256(cls, value: str) -> str:
        if SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("Invalid SHA-256")
        return value

    @field_validator("quality_category")
    @classmethod
    def dataset_category(cls, value: str) -> str:
        if value not in {"normal", "challenging", "unusable", "rejected"}:
            raise ValueError("Invalid quality category")
        return value

    @field_validator("yolo_sha256")
    @classmethod
    def yolo_digest(cls, value: str) -> str:
        if SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("Invalid YOLO SHA-256")
        return value


class StoredDatasetExportV1(StrictArtifactModel):
    object_key: StrictStr
    size_bytes: PositiveStrictInt
    sha256: StrictStr

    @field_validator("sha256")
    @classmethod
    def export_sha256(cls, value: str) -> str:
        if SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("Invalid SHA-256")
        return value


class StoredDatasetManifestV1(StrictArtifactModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    dataset_type: Annotated[str, Field(pattern="^image$")]
    job_id: UUID
    run_token: UUID
    created_at: datetime
    summary: DatasetSummaryV1
    recommended_indices: list[NonNegativeStrictInt]
    images: list[StoredDatasetImageV1] = Field(max_length=10_000)
    exports: dict[str, StoredDatasetExportV1]


class PublicDatasetImage(BaseModel):
    index: int
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    quality_category: str
    sharpness: float
    brightness: float
    underexposed_ratio: float
    overexposed_ratio: float
    resolution_usable: bool
    duplicate: bool
    access_url: str | None
    download_url: str | None


class PublicDatasetManifest(BaseModel):
    schema_version: int
    dataset_type: str
    job_id: UUID
    created_at: datetime
    summary: DatasetSummaryV1
    recommended_indices: list[int]
    images: list[PublicDatasetImage]
    accepted_download_url: str
    yolo_download_url: str
