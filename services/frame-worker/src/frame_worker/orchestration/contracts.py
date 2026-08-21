from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class JobStatus(StrEnum):
    PENDING_DISPATCH = "PENDING_DISPATCH"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SourceType(StrEnum):
    URL = "URL"
    UPLOAD = "UPLOAD"


@dataclass(frozen=True)
class JobRecord:
    id: UUID
    status: str
    source_type: str
    source_secret: str | None
    source_reference: dict[str, Any] | None
    processing_config: dict[str, Any]
    attempt_count: int
    run_token: UUID | None
    lease_expires_at: datetime | None
    result_reference: str | None
    result_summary: dict[str, Any] | None
    version: int


@dataclass(frozen=True)
class ClaimResult:
    job: JobRecord | None
    retry_later: bool = False
