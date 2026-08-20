from enum import StrEnum


class JobStatus(StrEnum):
    PENDING_DISPATCH = "PENDING_DISPATCH"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    def can_transition_to(self, target: "JobStatus") -> bool:
        return target in _ALLOWED_TRANSITIONS[self]


_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING_DISPATCH: frozenset({JobStatus.QUEUED}),
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING}),
    JobStatus.RUNNING: frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


class SourceType(StrEnum):
    URL = "URL"
    UPLOAD = "UPLOAD"


class FailureCode(StrEnum):
    UNSAFE_URL = "UNSAFE_URL"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    INVALID_VIDEO = "INVALID_VIDEO"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    DOWNLOAD_TIMEOUT = "DOWNLOAD_TIMEOUT"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    PROCESSING_FAILED = "PROCESSING_FAILED"


class OutboxEventType(StrEnum):
    PROCESS_VIDEO_JOB = "PROCESS_VIDEO_JOB"
