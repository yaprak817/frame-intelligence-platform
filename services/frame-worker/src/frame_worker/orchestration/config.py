# ruff: noqa: E501
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
    object_storage_bucket: str
    object_storage_region: str
    object_storage_addressing_style: str
    max_download_bytes: int
    lease_seconds: float
    heartbeat_interval_seconds: float
    visibility_timeout_seconds: int
    worker_concurrency: int
    processing_temp_root: Path | None
    task_queue: str = "video-processing"
    redis_keyprefix: str = ""
    export_lease_seconds: float = 300
    export_heartbeat_interval_seconds: float = 30
    export_soft_time_limit_seconds: int = 1800
    export_hard_time_limit_seconds: int = 1860
    export_s3_connect_timeout_seconds: float = 5
    export_s3_read_timeout_seconds: float = 30
    export_max_frames: int = 1000
    export_max_total_source_bytes: int = 2 * 1024 * 1024 * 1024
    export_max_zip_bytes: int = 2 * 1024 * 1024 * 1024
    export_max_temp_bytes: int = 2_200_000_000
    export_min_temp_free_bytes: int = 256 * 1024 * 1024
    export_stale_temp_seconds: int = 7200
    dataset_max_images: int = 1000
    dataset_max_file_bytes: int = 50 * 1024 * 1024
    dataset_max_total_bytes: int = 2 * 1024 * 1024 * 1024
    dataset_max_pixels: int = 100_000_000
    dataset_min_pixels: int = 1024
    dataset_zip_max_entries: int = 1200
    dataset_zip_max_compressed_bytes: int = 2 * 1024 * 1024 * 1024
    dataset_zip_max_entry_compressed_bytes: int = 50 * 1024 * 1024
    dataset_zip_max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    dataset_zip_max_ratio: float = 100.0
    dataset_export_max_bytes: int = 2 * 1024 * 1024 * 1024
    dataset_normal_sharpness: float = 100.0
    dataset_unusable_sharpness: float = 5.0
    dataset_min_brightness: float = 35.0
    dataset_max_brightness: float = 225.0
    dataset_unusable_brightness: float = 5.0
    dataset_soft_time_limit_seconds: int = 1800
    dataset_hard_time_limit_seconds: int = 1860
    dataset_max_temp_bytes: int = 4_400_000_000
    dataset_min_temp_free_bytes: int = 268_435_456

    def __post_init__(self) -> None:
        if self.dataset_max_total_bytes < self.dataset_max_file_bytes:
            raise ValueError("Dataset total limit must cover one file")
        if self.dataset_min_pixels >= self.dataset_max_pixels:
            raise ValueError("Dataset pixel limits are inconsistent")
        if self.dataset_zip_max_entries < self.dataset_max_images:
            raise ValueError("ZIP entry limit must cover the image limit")
        if self.dataset_zip_max_uncompressed_bytes < self.dataset_max_file_bytes:
            raise ValueError("ZIP total limit must cover one file")
        if not (
            self.dataset_unusable_sharpness < self.dataset_normal_sharpness
            and self.dataset_unusable_brightness
            < self.dataset_min_brightness
            < self.dataset_max_brightness
            < 255
        ):
            raise ValueError("Dataset quality thresholds are inconsistent")
        if self.dataset_hard_time_limit_seconds <= self.dataset_soft_time_limit_seconds:
            raise ValueError("Dataset hard time limit must exceed soft time limit")
        if self.dataset_max_temp_bytes < self.dataset_max_total_bytes:
            raise ValueError("Dataset temp limit must cover source bytes")

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
        export_lease = _positive_float("FRAME_EXPORT_LEASE_SECONDS", "300")
        export_heartbeat = _positive_float(
            "FRAME_EXPORT_HEARTBEAT_INTERVAL_SECONDS", "30"
        )
        if export_heartbeat >= export_lease / 2:
            raise ValueError(
                "Export heartbeat interval must be less than half the lease"
            )
        soft_limit = _positive_int("FRAME_EXPORT_SOFT_TIME_LIMIT_SECONDS", "1800")
        hard_limit = _positive_int("FRAME_EXPORT_HARD_TIME_LIMIT_SECONDS", "1860")
        if hard_limit <= soft_limit:
            raise ValueError("Export hard time limit must exceed soft time limit")
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
            object_storage_bucket=os.environ.get(
                "OBJECT_STORAGE_BUCKET", "frame-intelligence"
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
            task_queue=os.environ.get("CELERY_TASK_QUEUE", "video-processing"),
            redis_keyprefix=os.environ.get("CELERY_REDIS_KEYPREFIX", ""),
            processing_temp_root=Path(temp_root).resolve() if temp_root else None,
            export_lease_seconds=export_lease,
            export_heartbeat_interval_seconds=export_heartbeat,
            export_soft_time_limit_seconds=soft_limit,
            export_hard_time_limit_seconds=hard_limit,
            export_s3_connect_timeout_seconds=_positive_float(
                "FRAME_EXPORT_S3_CONNECT_TIMEOUT_SECONDS", "5"
            ),
            export_s3_read_timeout_seconds=_positive_float(
                "FRAME_EXPORT_S3_READ_TIMEOUT_SECONDS", "30"
            ),
            export_max_frames=_positive_int("FRAME_EXPORT_MAX_FRAMES", "1000"),
            export_max_total_source_bytes=_positive_int(
                "FRAME_EXPORT_MAX_TOTAL_BYTES", "2147483648"
            ),
            export_max_zip_bytes=_positive_int(
                "FRAME_EXPORT_MAX_ZIP_BYTES", "2147483648"
            ),
            export_max_temp_bytes=_positive_int(
                "FRAME_EXPORT_MAX_TEMP_BYTES", "2200000000"
            ),
            export_min_temp_free_bytes=_positive_int(
                "FRAME_EXPORT_MIN_TEMP_FREE_BYTES", "268435456"
            ),
            export_stale_temp_seconds=_positive_int(
                "FRAME_EXPORT_STALE_TEMP_SECONDS", "7200"
            ),
            dataset_max_images=_positive_int("IMAGE_DATASET_MAX_IMAGES", "1000"),
            dataset_max_file_bytes=_positive_int(
                "IMAGE_DATASET_MAX_FILE_BYTES", "52428800"
            ),
            dataset_max_total_bytes=_positive_int(
                "IMAGE_DATASET_MAX_TOTAL_BYTES", "2147483648"
            ),
            dataset_max_pixels=_positive_int("IMAGE_DATASET_MAX_PIXELS", "100000000"),
            dataset_min_pixels=_positive_int("IMAGE_DATASET_MIN_PIXELS", "1024"),
            dataset_zip_max_entries=_positive_int(
                "IMAGE_DATASET_ZIP_MAX_ENTRIES", "1200"
            ),
            dataset_zip_max_compressed_bytes=_positive_int(
                "IMAGE_DATASET_ZIP_MAX_COMPRESSED_BYTES", "2147483648"
            ),
            dataset_zip_max_entry_compressed_bytes=_positive_int(
                "IMAGE_DATASET_ZIP_MAX_ENTRY_COMPRESSED_BYTES", "52428800"
            ),
            dataset_zip_max_uncompressed_bytes=_positive_int(
                "IMAGE_DATASET_ZIP_MAX_UNCOMPRESSED_BYTES", "2147483648"
            ),
            dataset_zip_max_ratio=_positive_float("IMAGE_DATASET_ZIP_MAX_RATIO", "100"),
            dataset_export_max_bytes=_positive_int(
                "IMAGE_DATASET_EXPORT_MAX_BYTES", "2147483648"
            ),
            dataset_normal_sharpness=_positive_float(
                "IMAGE_DATASET_NORMAL_SHARPNESS", "100"
            ),
            dataset_unusable_sharpness=_positive_float(
                "IMAGE_DATASET_UNUSABLE_SHARPNESS", "5"
            ),
            dataset_min_brightness=_positive_float(
                "IMAGE_DATASET_MIN_BRIGHTNESS", "35"
            ),
            dataset_max_brightness=_positive_float(
                "IMAGE_DATASET_MAX_BRIGHTNESS", "225"
            ),
            dataset_unusable_brightness=_positive_float(
                "IMAGE_DATASET_UNUSABLE_BRIGHTNESS", "5"
            ),
            dataset_soft_time_limit_seconds=_positive_int(
                "IMAGE_DATASET_SOFT_TIME_LIMIT_SECONDS", "1800"
            ),
            dataset_hard_time_limit_seconds=_positive_int(
                "IMAGE_DATASET_HARD_TIME_LIMIT_SECONDS", "1860"
            ),
            dataset_max_temp_bytes=_positive_int(
                "IMAGE_DATASET_MAX_TEMP_BYTES", "4400000000"
            ),
            dataset_min_temp_free_bytes=_positive_int(
                "IMAGE_DATASET_MIN_TEMP_FREE_BYTES", "268435456"
            ),
        )
