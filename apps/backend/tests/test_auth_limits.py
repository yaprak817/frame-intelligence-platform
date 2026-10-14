import base64

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.security.rate_limit import protected_group

TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def settings(**overrides):
    values = {
        "job_source_encryption_key": TEST_KEY,
        "environment": "test",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/login",
        "/api/v1/auth/register",
    ],
)
def test_auth_mutations_have_dedicated_rate_limit(path):
    assert protected_group("POST", path) == (
        "auth",
        "rate_limit_auth_requests",
    )


def test_auth_reads_and_logout_do_not_use_login_budget():
    assert protected_group("GET", "/api/v1/auth/me") is None
    assert protected_group("POST", "/api/v1/auth/logout") is None


@pytest.mark.parametrize(
    "ttl",
    [299, 30 * 24 * 60 * 60 + 1],
)
def test_session_ttl_is_bounded(ttl):
    with pytest.raises(ValidationError):
        settings(auth_session_ttl_seconds=ttl)


@pytest.mark.parametrize("ttl", [300, 7 * 24 * 60 * 60, 30 * 24 * 60 * 60])
def test_session_ttl_accepts_safe_range(ttl):
    assert settings(auth_session_ttl_seconds=ttl).auth_session_ttl_seconds == ttl


@pytest.mark.parametrize("limit", [0, 10_001])
def test_auth_rate_limit_is_bounded(limit):
    with pytest.raises(ValidationError):
        settings(rate_limit_auth_requests=limit)


def test_default_auth_rate_limit_is_ten_per_window():
    assert settings().rate_limit_auth_requests == 10
