import re
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.domain.jobs import FailureCode, JobStatus, SourceType

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

IdempotencyKey = Annotated[
    str,
    StringConstraints(min_length=8, max_length=128, pattern=IDEMPOTENCY_KEY_PATTERN),
]


def validate_submission_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL is invalid") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("URL scheme must be http or https")
    if not hostname:
        raise ValueError("URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    if any(character.isspace() for character in value):
        raise ValueError("URL is invalid")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port is invalid")
    return value


def safe_url_display(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", "", "")
    )


class ProcessingConfigRequest(BaseModel):
    candidate_fps: float = Field(default=5.0, gt=0, le=60)
    selection_window_seconds: float = Field(default=1.0, gt=0, le=3600)


class URLJobRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)
    processing: ProcessingConfigRequest = Field(default_factory=ProcessingConfigRequest)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return validate_submission_url(value)


class JobSubmissionResponse(BaseModel):
    job_id: UUID
    status: JobStatus
    status_url: str


class JobFailureResponse(BaseModel):
    code: FailureCode | str
    message: str


class JobStatusResponse(BaseModel):
    id: UUID
    status: JobStatus
    source_type: SourceType
    source: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure: JobFailureResponse | None
    result: str | None
