import base64
from uuid import uuid4

import pytest

from app.core.config import decode_encryption_key
from app.security.source_secrets import SourceSecretCipher, SourceSecretError


def test_encryption_round_trip_uses_job_id_as_associated_data(
    cipher: SourceSecretCipher,
) -> None:
    job_id = uuid4()
    raw_url = "https://example.com/watch?token=secret"

    protected = cipher.encrypt(raw_url, job_id)

    assert raw_url not in protected
    assert cipher.decrypt(protected, job_id) == raw_url
    with pytest.raises(SourceSecretError, match="could not be decrypted"):
        cipher.decrypt(protected, uuid4())


def test_encryption_uses_a_new_nonce(cipher: SourceSecretCipher) -> None:
    job_id = uuid4()
    raw_url = "https://example.com/watch?token=secret"

    assert cipher.encrypt(raw_url, job_id) != cipher.encrypt(raw_url, job_id)


def test_wrong_key_failure_does_not_expose_plaintext() -> None:
    first = SourceSecretCipher(base64.urlsafe_b64encode(b"A" * 32).decode())
    second = SourceSecretCipher(base64.urlsafe_b64encode(b"B" * 32).decode())
    job_id = uuid4()
    protected = first.encrypt("https://example.com/?token=secret", job_id)

    with pytest.raises(SourceSecretError) as captured:
        second.decrypt(protected, job_id)

    assert "token=secret" not in str(captured.value)


@pytest.mark.parametrize(
    "value",
    ["not-base64", base64.urlsafe_b64encode(b"short").decode()],
)
def test_invalid_configured_key_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="JOB_SOURCE_ENCRYPTION_KEY"):
        decode_encryption_key(value)
