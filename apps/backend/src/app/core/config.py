import base64
import binascii
import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def decode_encryption_key(value: str) -> bytes:
    """Decode and validate a URL-safe base64 AES-256 key."""

    if not re.fullmatch(r"[A-Za-z0-9_-]{43}=", value):
        raise ValueError(
            "JOB_SOURCE_ENCRYPTION_KEY must be canonical padded URL-safe base64"
        )
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("JOB_SOURCE_ENCRYPTION_KEY must be URL-safe base64") from error
    if len(decoded) != 32:
        raise ValueError("JOB_SOURCE_ENCRYPTION_KEY must decode to exactly 32 bytes")
    if base64.urlsafe_b64encode(decoded).decode() != value:
        raise ValueError(
            "JOB_SOURCE_ENCRYPTION_KEY must be canonical padded URL-safe base64"
        )
    return decoded


class Settings(BaseSettings):
    app_name: str = "Frame Intelligence API"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "staging", "production"] = "development"
    database_url: str = (
        "postgresql+psycopg://frame_user:change_me@localhost:5432/frame_intelligence"
    )
    job_source_encryption_key: str
    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_external_endpoint: str = "http://localhost:9000"
    object_storage_access_key: str = "frame_admin"
    object_storage_secret_key: str = "change_me"
    object_storage_bucket: str = "frame-intelligence"
    object_storage_region: str = "us-east-1"
    object_storage_addressing_style: str = "path"
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    object_storage_multipart_chunk_bytes: int = 8 * 1024 * 1024
    result_artifact_url_ttl_seconds: int = 300
    celery_broker_url: str = "redis://localhost:6379/0"
    redis_url: str | None = None
    cors_allow_origins: list[str] = Field(default_factory=list)
    cors_allow_credentials: bool = False
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    internal_proxy_shared_secret: str = ""
    rate_limit_submission_requests: int = 10
    rate_limit_result_requests: int = 60
    rate_limit_window_seconds: int = 60
    dependency_timeout_seconds: float = 2.0
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 10
    outbox_backoff_base_seconds: float = 1.0
    outbox_backoff_max_seconds: float = 60.0

    @field_validator("job_source_encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        decode_encryption_key(value)
        return value

    @field_validator("cors_allow_origins")
    @classmethod
    def validate_cors_origins(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if value == "*":
                origin = value
            else:
                parsed = urlsplit(value)
                try:
                    port = parsed.port
                except ValueError as error:
                    raise ValueError(
                        "CORS_ALLOW_ORIGINS contains an invalid origin"
                    ) from error
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path not in {"", "/"}
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("CORS_ALLOW_ORIGINS contains an invalid origin")
                host = parsed.hostname.lower()
                if ":" in host:
                    host = f"[{host}]"
                origin = f"{parsed.scheme.lower()}://{host}"
                if port is not None:
                    origin += f":{port}"
            if origin not in normalized:
                normalized.append(origin)
        return normalized

    @model_validator(mode="after")
    def validate_storage(self) -> "Settings":
        internal_url = urlsplit(self.object_storage_endpoint)
        external_url = urlsplit(self.object_storage_external_endpoint)
        scheme = internal_url.scheme
        external_scheme = external_url.scheme
        if scheme not in {"http", "https"} or external_scheme not in {"http", "https"}:
            raise ValueError("Object storage endpoints must use http or https")
        for endpoint in (internal_url, external_url):
            if (
                not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.path not in {"", "/"}
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("Object storage endpoint is invalid")
        if (
            self.environment in {"staging", "production"}
            and scheme != "https"
            and self.object_storage_endpoint != "http://minio:9000"
        ):
            raise ValueError(
                "OBJECT_STORAGE_ENDPOINT must use https unless it targets a private "
                "container service hostname"
            )
        if self.environment in {"staging", "production"} and external_scheme != "https":
            raise ValueError(
                "OBJECT_STORAGE_EXTERNAL_ENDPOINT must use https outside development"
            )
        if self.object_storage_addressing_style not in {"path", "virtual"}:
            raise ValueError("OBJECT_STORAGE_ADDRESSING_STYLE must be path or virtual")
        if self.max_upload_bytes <= 0:
            raise ValueError("MAX_UPLOAD_BYTES must be greater than zero")
        if self.object_storage_multipart_chunk_bytes < 5 * 1024 * 1024:
            raise ValueError(
                "OBJECT_STORAGE_MULTIPART_CHUNK_BYTES must be at least 5 MiB"
            )
        if not 30 <= self.result_artifact_url_ttl_seconds <= 900:
            raise ValueError(
                "RESULT_ARTIFACT_URL_TTL_SECONDS must be between 30 and 900"
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
        if self.redis_url is None:
            self.redis_url = self.celery_broker_url
        if self.cors_allow_credentials and "*" in self.cors_allow_origins:
            raise ValueError(
                "CORS_ALLOW_ORIGINS cannot contain wildcard when credentials "
                "are enabled"
            )
        for cidr in self.trusted_proxy_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as error:
                raise ValueError(
                    "TRUSTED_PROXY_CIDRS contains an invalid network"
                ) from error
        if not 1 <= self.rate_limit_submission_requests <= 10_000:
            raise ValueError(
                "RATE_LIMIT_SUBMISSION_REQUESTS must be between 1 and 10000"
            )
        if not 1 <= self.rate_limit_result_requests <= 100_000:
            raise ValueError("RATE_LIMIT_RESULT_REQUESTS must be between 1 and 100000")
        if not 1 <= self.rate_limit_window_seconds <= 86_400:
            raise ValueError("RATE_LIMIT_WINDOW_SECONDS must be between 1 and 86400")
        if not 0.1 <= self.dependency_timeout_seconds <= 10.0:
            raise ValueError("DEPENDENCY_TIMEOUT_SECONDS must be between 0.1 and 10")
        if self.environment in {"staging", "production"}:
            self._validate_production_secrets()
        return self

    def _validate_production_secrets(self) -> None:
        placeholders = {
            "change_me",
            "changeme",
            "password",
            "replace_me",
            "frame_admin",
            "secret",
        }
        credentials = {
            "DATABASE_URL": _url_password(self.database_url),
            "OBJECT_STORAGE_ACCESS_KEY": self.object_storage_access_key,
            "OBJECT_STORAGE_SECRET_KEY": self.object_storage_secret_key,
            "INTERNAL_PROXY_SHARED_SECRET": self.internal_proxy_shared_secret,
        }
        for name, value in credentials.items():
            normalized = (value or "").strip().lower()
            if (
                not normalized
                or normalized in placeholders
                or "replace" in normalized
                or (name == "INTERNAL_PROXY_SHARED_SECRET" and len(value or "") < 32)
            ):
                raise ValueError(f"{name} must be configured securely in production")
        key = decode_encryption_key(self.job_source_encryption_key)
        if len(set(key)) < 8:
            raise ValueError(
                "JOB_SOURCE_ENCRYPTION_KEY must be configured securely in production"
            )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )


def _url_password(value: str) -> str | None:
    try:
        return urlsplit(
            value.replace("postgresql+psycopg://", "postgresql://", 1)
        ).password
    except ValueError:
        return None


settings = Settings()
