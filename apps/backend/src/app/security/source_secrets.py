import base64
import hashlib
import hmac
import json
import os
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import decode_encryption_key

_NONCE_BYTES = 12
_FINGERPRINT_CONTEXT = b"frame-intelligence:job-request-fingerprint:v1"


class SourceSecretError(RuntimeError):
    """Raised when protected source data cannot be safely processed."""


class SourceSecretCipher:
    def __init__(self, encoded_key: str) -> None:
        self._key = decode_encryption_key(encoded_key)
        self._cipher = AESGCM(self._key)
        self._fingerprint_key = hmac.digest(
            self._key, _FINGERPRINT_CONTEXT, hashlib.sha256
        )

    def encrypt(self, raw_url: str, job_id: UUID) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce,
            raw_url.encode("utf-8"),
            job_id.bytes,
        )
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, protected_url: str, job_id: UUID) -> str:
        try:
            payload = base64.b64decode(protected_url, altchars=b"-_", validate=True)
            if len(payload) <= _NONCE_BYTES:
                raise ValueError
            plaintext = self._cipher.decrypt(
                payload[:_NONCE_BYTES],
                payload[_NONCE_BYTES:],
                job_id.bytes,
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise SourceSecretError("Source secret could not be decrypted") from error

    def fingerprint(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hmac.new(self._fingerprint_key, canonical, hashlib.sha256).hexdigest()
