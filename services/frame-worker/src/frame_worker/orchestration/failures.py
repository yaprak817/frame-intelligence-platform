from dataclasses import dataclass

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import OperationalError

from frame_worker.ingestion.errors import (
    InvalidVideoSourceError,
    UnsafeVideoURLError,
    UnsupportedVideoSourceError,
    VideoDownloadError,
    VideoTooLargeError,
)


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    retryable: bool


SAFE_MESSAGES = {
    "UNSAFE_URL": "The video URL is not allowed.",
    "UNSUPPORTED_SOURCE": "The video source is not supported.",
    "INVALID_VIDEO": "The source is not a valid video.",
    "SOURCE_TOO_LARGE": "The video exceeds the allowed size.",
    "DOWNLOAD_TIMEOUT": "The video download timed out.",
    "DOWNLOAD_FAILED": "The video could not be downloaded.",
    "STORAGE_UNAVAILABLE": "Object storage is temporarily unavailable.",
    "PROCESSING_FAILED": "Video processing failed.",
}


def classify_failure(error: BaseException, source_type: str) -> Failure:
    if isinstance(error, UnsafeVideoURLError):
        code = "UNSAFE_URL"
    elif isinstance(error, UnsupportedVideoSourceError):
        code = "UNSUPPORTED_SOURCE"
    elif isinstance(error, VideoTooLargeError):
        code = "SOURCE_TOO_LARGE"
    elif isinstance(error, InvalidVideoSourceError):
        code = "INVALID_VIDEO"
    elif isinstance(error, (OperationalError, BotoCoreError, ClientError)):
        code = "STORAGE_UNAVAILABLE" if source_type == "UPLOAD" else "DOWNLOAD_FAILED"
        return Failure(code, SAFE_MESSAGES[code], True)
    elif isinstance(error, VideoDownloadError):
        if source_type == "UPLOAD":
            code = "STORAGE_UNAVAILABLE"
            return Failure(code, SAFE_MESSAGES[code], True)
        if _caused_by_timeout(error):
            code = "DOWNLOAD_TIMEOUT"
            return Failure(code, SAFE_MESSAGES[code], True)
        if _is_transient_download(error):
            code = "DOWNLOAD_FAILED"
            return Failure(code, SAFE_MESSAGES[code], True)
        code = "DOWNLOAD_FAILED"
    elif isinstance(error, (ValueError, FileNotFoundError)):
        code = "INVALID_VIDEO"
    else:
        code = "PROCESSING_FAILED"
    return Failure(code, SAFE_MESSAGES[code], False)


def _caused_by_timeout(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, (TimeoutError, httpx.TimeoutException)):
            return True
        current = current.__cause__
    return False


def _is_transient_download(error: BaseException) -> bool:
    message = str(error)
    if "HTTP status 429" in message or "HTTP status 5" in message:
        return True
    current: BaseException | None = error.__cause__
    while current is not None:
        if isinstance(current, httpx.NetworkError):
            return True
        current = current.__cause__
    return False
