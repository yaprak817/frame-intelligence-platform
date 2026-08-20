import base64
import binascii

from pydantic import field_validator
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

    @field_validator("job_source_encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        decode_encryption_key(value)
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
