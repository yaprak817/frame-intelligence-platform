import base64

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def secure_values() -> dict[str, object]:
    return {
        "environment": "production",
        "database_url": "postgresql+psycopg://service:strong-db-pass@db/prod",
        "job_source_encryption_key": base64.urlsafe_b64encode(
            bytes(range(32))
        ).decode(),
        "internal_proxy_shared_secret": "proxy-secret-0123456789-ABCDEFGHIJ",
        "object_storage_endpoint": "https://storage.internal",
        "object_storage_external_endpoint": "https://storage.example.test",
        "object_storage_access_key": "prod-access-92",
        "object_storage_secret_key": "prod-secret-92-long",
    }


def test_development_accepts_safe_test_key() -> None:
    settings = Settings(
        job_source_encryption_key=base64.urlsafe_b64encode(b"T" * 32).decode()
    )
    assert settings.environment == "development"


@pytest.mark.parametrize(
    "value",
    [
        base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        base64.urlsafe_b64encode(bytes(range(32))).decode() + "=",
        base64.b64encode(b"\xff" * 32).decode(),
        base64.urlsafe_b64encode(bytes(range(32))).decode()[:-2] + "9=",
        base64.urlsafe_b64encode(b"short").decode(),
        base64.urlsafe_b64encode(b"x" * 33).decode(),
        "!" * 43 + "=",
    ],
)
def test_encryption_key_requires_canonical_padded_urlsafe_base64(value: str) -> None:
    with pytest.raises(ValidationError, match="JOB_SOURCE_ENCRYPTION_KEY"):
        Settings(job_source_encryption_key=value)


def test_secure_production_config_is_accepted() -> None:
    assert Settings(**secure_values()).environment == "production"


def test_staging_uses_production_security_checks() -> None:
    values = secure_values()
    values["environment"] = "staging"
    values["object_storage_secret_key"] = "change_me"
    with pytest.raises(ValidationError):
        Settings(**values)


@pytest.mark.parametrize("environment", ["prod", "Production", "unknown"])
def test_unknown_environment_is_rejected(environment) -> None:
    values = secure_values()
    values["environment"] = environment
    with pytest.raises(ValidationError):
        Settings(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_storage_secret_key", ""),
        ("object_storage_secret_key", "change_me"),
        ("object_storage_access_key", "frame_admin"),
    ],
)
def test_production_rejects_missing_or_placeholder_credentials(field, value) -> None:
    values = secure_values()
    values[field] = value
    with pytest.raises(ValidationError) as raised:
        Settings(**values)
    assert "prod-secret-92-long" not in str(raised.value)


def test_production_rejects_predictable_encryption_key() -> None:
    values = secure_values()
    values["job_source_encryption_key"] = base64.urlsafe_b64encode(b"A" * 32).decode()
    with pytest.raises(ValidationError):
        Settings(**values)


def test_credentials_with_wildcard_cors_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            job_source_encryption_key=base64.urlsafe_b64encode(b"T" * 32).decode(),
            cors_allow_origins=["*"],
            cors_allow_credentials=True,
        )


@pytest.mark.parametrize(
    "field",
    [
        "rate_limit_submission_requests",
        "rate_limit_result_requests",
        "rate_limit_window_seconds",
    ],
)
def test_invalid_rate_limit_values_are_rejected(field) -> None:
    with pytest.raises(ValidationError):
        Settings(
            job_source_encryption_key=base64.urlsafe_b64encode(b"T" * 32).decode(),
            **{field: 0},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rate_limit_submission_requests", 10_001),
        ("rate_limit_result_requests", 100_001),
        ("rate_limit_window_seconds", 86_401),
        ("dependency_timeout_seconds", 0),
        ("dependency_timeout_seconds", 10.1),
    ],
)
def test_security_config_upper_bounds_are_enforced(field, value) -> None:
    with pytest.raises(ValidationError):
        Settings(
            job_source_encryption_key=base64.urlsafe_b64encode(b"T" * 32).decode(),
            **{field: value},
        )


def test_cors_origins_are_normalized_and_deduplicated() -> None:
    settings = Settings(
        job_source_encryption_key=base64.urlsafe_b64encode(b"T" * 32).decode(),
        cors_allow_origins=[
            "HTTP://LOCALHOST:3000/",
            "http://localhost:3000",
            "https://[2001:db8::1]:8443/",
        ],
    )
    assert settings.cors_allow_origins == [
        "http://localhost:3000",
        "https://[2001:db8::1]:8443",
    ]


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://example.com",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com?query=1",
        "https://example.com/#fragment",
        "not-an-origin",
    ],
)
def test_invalid_cors_origins_are_rejected(origin) -> None:
    with pytest.raises(ValidationError):
        Settings(
            job_source_encryption_key=base64.urlsafe_b64encode(b"T" * 32).decode(),
            cors_allow_origins=[origin],
        )


def test_production_allows_private_container_storage_endpoint() -> None:
    values = secure_values()
    values["object_storage_endpoint"] = "http://minio:9000"

    settings = Settings(**values)

    assert settings.object_storage_endpoint == "http://minio:9000"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://storage:9000",
        "http://localhost:9000",
        "http://127.0.0.1:9000",
        "http://minio.example:9000",
        "http://minio",
        "http://minio:9001",
        "http://minio:9000/path",
        "http://minio:9000/",
    ],
)
def test_production_rejects_other_cleartext_storage_hosts(endpoint: str) -> None:
    values = secure_values()
    values["object_storage_endpoint"] = endpoint

    with pytest.raises(ValueError):
        Settings(**values)


@pytest.mark.parametrize("secret", ["", "short", "replace_with_random_value"])
def test_production_rejects_invalid_internal_proxy_secret(secret: str) -> None:
    values = secure_values()
    values["internal_proxy_shared_secret"] = secret
    with pytest.raises(ValidationError, match="INTERNAL_PROXY_SHARED_SECRET"):
        Settings(**values)
