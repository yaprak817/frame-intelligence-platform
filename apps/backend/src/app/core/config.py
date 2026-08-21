import base64
import binascii
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def decode_encryption_key(value: str) -> bytes:
    """Decode and validate a URL-safe base64 AES-256 key."""

    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("JOB_SOURCE_ENCRYPTION_KEY must be URL-safe base64") from error
    if len(decoded) != 32:
        raise ValueError("JOB_SOURCE_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return decoded


class Settings(BaseSettings):
    app_name: str = "Frame Intelligence API"
    app_version: str = "0.1.0"
    environment: str = "development"
    database_url: str = (
        "postgresql+psycopg://frame_user:change_me@localhost:5432/frame_intelligence"
    )
    job_source_encryption_key: str
    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_access_key: str = "frame_admin"
    object_storage_secret_key: str = "change_me"
    object_storage_bucket: str = "frame-intelligence"
    object_storage_region: str = "us-east-1"
    object_storage_addressing_style: str = "path"
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    object_storage_multipart_chunk_bytes: int = 8 * 1024 * 1024
    celery_broker_url: str = "redis://localhost:6379/0"
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 10
    outbox_backoff_base_seconds: float = 1.0
    outbox_backoff_max_seconds: float = 60.0

    @field_validator("job_source_encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        decode_encryption_key(value)
        return value

    @model_validator(mode="after")
    def validate_storage(self) -> "Settings":
        scheme = urlsplit(self.object_storage_endpoint).scheme
        if scheme not in {"http", "https"}:
            raise ValueError("OBJECT_STORAGE_ENDPOINT must use http or https")
        if (
            self.environment.lower() not in {"development", "test"}
            and scheme != "https"
        ):
            raise ValueError(
                "OBJECT_STORAGE_ENDPOINT must use https outside development"
            )
        if self.object_storage_addressing_style not in {"path", "virtual"}:
            raise ValueError("OBJECT_STORAGE_ADDRESSING_STYLE must be path or virtual")
        if self.max_upload_bytes <= 0:
            raise ValueError("MAX_UPLOAD_BYTES must be greater than zero")
        if self.object_storage_multipart_chunk_bytes < 5 * 1024 * 1024:
            raise ValueError(
                "OBJECT_STORAGE_MULTIPART_CHUNK_BYTES must be at least 5 MiB"
            )
        if self.outbox_poll_interval_seconds <= 0:
            raise ValueError("OUTBOX_POLL_INTERVAL_SECONDS must be greater than zero")
        if not 1 <= self.outbox_batch_size <= 100:
            raise ValueError("OUTBOX_BATCH_SIZE must be between 1 and 100")
        if self.outbox_backoff_base_seconds <= 0:
            raise ValueError("OUTBOX_BACKOFF_BASE_SECONDS must be greater than zero")
        if self.outbox_backoff_max_seconds < self.outbox_backoff_base_seconds:
            raise ValueError(
                "OUTBOX_BACKOFF_MAX_SECONDS must be at least the base backoff"
            )
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
