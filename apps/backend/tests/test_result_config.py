import base64

import pytest
from pydantic import ValidationError

from app.core.config import Settings

TEST_KEY = base64.urlsafe_b64encode(b"R" * 32).decode()


@pytest.mark.parametrize("ttl", [30, 300, 900])
def test_result_artifact_ttl_accepts_documented_range(ttl: int) -> None:
    settings = Settings(
        _env_file=None,
        job_source_encryption_key=TEST_KEY,
        environment="test",
        result_artifact_url_ttl_seconds=ttl,
    )
    assert settings.result_artifact_url_ttl_seconds == ttl


@pytest.mark.parametrize("ttl", [29, 901])
def test_result_artifact_ttl_rejects_values_outside_range(ttl: int) -> None:
    with pytest.raises(ValidationError, match="between 30 and 900"):
        Settings(
            _env_file=None,
            job_source_encryption_key=TEST_KEY,
            environment="test",
            result_artifact_url_ttl_seconds=ttl,
        )
