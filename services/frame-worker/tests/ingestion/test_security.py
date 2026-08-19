import pytest

from frame_worker.ingestion.errors import UnsafeVideoURLError
from frame_worker.ingestion.security import validate_safe_http_url


def resolver_for(address: str):
    return lambda _hostname, _port: [address]


def test_localhost_is_blocked() -> None:
    with pytest.raises(UnsafeVideoURLError, match="Localhost"):
        validate_safe_http_url("http://localhost/video.mp4")


def test_private_ipv4_is_blocked() -> None:
    with pytest.raises(UnsafeVideoURLError, match="non-public"):
        validate_safe_http_url(
            "https://video.example/file.mp4",
            resolver_for("10.0.0.8"),
        )


def test_private_ipv6_is_blocked() -> None:
    with pytest.raises(UnsafeVideoURLError, match="non-public"):
        validate_safe_http_url(
            "https://video.example/file.mp4",
            resolver_for("fd00::1"),
        )


def test_url_credentials_are_blocked() -> None:
    with pytest.raises(UnsafeVideoURLError, match="credentials"):
        validate_safe_http_url("https://user:secret@example.com/video.mp4")


def test_public_http_url_is_allowed() -> None:
    parsed = validate_safe_http_url(
        "https://video.example/file.mp4",
        resolver_for("93.184.216.34"),
    )

    assert parsed.hostname == "video.example"
