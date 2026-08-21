import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _positive_float(name: str, default: str) -> float:
    value = float(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _positive_int(name: str, default: str) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def decode_encryption_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("JOB_SOURCE_ENCRYPTION_KEY must be URL-safe base64") from error
    if len(decoded) != 32:
        raise ValueError("JOB_SOURCE_ENCRYPTION_KEY must decode to 32 bytes")
    return decoded


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    celery_broker_url: str
    job_source_encryption_key: str
    object_storage_endpoint: str
    object_storage_access_key: str
    object_storage_secret_key: str
    object_storage_region: str
    object_storage_addressing_style: str
    max_download_bytes: int
    lease_seconds: float
    heartbeat_interval_seconds: float
    visibility_timeout_seconds: int
    worker_concurrency: int
    processing_temp_root: Path | None

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        key = os.environ["JOB_SOURCE_ENCRYPTION_KEY"]
        decode_encryption_key(key)
        lease = _positive_float("JOB_LEASE_SECONDS", "900")
        heartbeat = _positive_float("JOB_HEARTBEAT_INTERVAL_SECONDS", "60")
        if heartbeat >= lease / 2:
            raise ValueError("Heartbeat interval must be less than half the lease")
        endpoint = os.environ.get("OBJECT_STORAGE_ENDPOINT", "http://localhost:9000")
        if urlsplit(endpoint).scheme not in {"http", "https"}:
            raise ValueError("OBJECT_STORAGE_ENDPOINT must use http or https")
        temp_root = os.environ.get("PROCESSING_TEMP_ROOT")
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL",
                "postgresql+psycopg://frame_user:change_me@localhost:5432/frame_intelligence",
            ),
            celery_broker_url=os.environ.get(
                "CELERY_BROKER_URL", "redis://localhost:6379/0"
            ),
            job_source_encryption_key=key,
            object_storage_endpoint=endpoint,
            object_storage_access_key=os.environ.get(
                "OBJECT_STORAGE_ACCESS_KEY", "frame_admin"
            ),
            object_storage_secret_key=os.environ.get(
                "OBJECT_STORAGE_SECRET_KEY", "change_me"
            ),
            object_storage_region=os.environ.get("OBJECT_STORAGE_REGION", "us-east-1"),
            object_storage_addressing_style=os.environ.get(
                "OBJECT_STORAGE_ADDRESSING_STYLE", "path"
            ),
            max_download_bytes=_positive_int("MAX_DOWNLOAD_BYTES", "2147483648"),
            lease_seconds=lease,
            heartbeat_interval_seconds=heartbeat,
            visibility_timeout_seconds=_positive_int(
                "CELERY_VISIBILITY_TIMEOUT_SECONDS", "7200"
            ),
            worker_concurrency=_positive_int("CELERY_WORKER_CONCURRENCY", "1"),
            processing_temp_root=Path(temp_root).resolve() if temp_root else None,
        )
