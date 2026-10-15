from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.dependencies import require_safe_origin


def make_request(
    *,
    method="POST",
    origin=None,
    referer=None,
    cors_origins=None,
):
    headers = [(b"host", b"app.example")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if referer is not None:
        headers.append((b"referer", referer.encode()))

    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(cors_allow_origins=cors_origins or [])
        )
    )

    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "server": ("app.example", 443),
            "path": "/api/v1/auth/login",
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "app": app,
        }
    )


def test_same_origin_mutation_is_allowed():
    require_safe_origin(make_request(origin="https://app.example"))


def test_configured_frontend_origin_is_allowed():
    require_safe_origin(
        make_request(
            origin="https://frontend.example",
            cors_origins=["https://frontend.example"],
        )
    )


def test_cross_origin_mutation_is_rejected():
    with pytest.raises(HTTPException) as error:
        require_safe_origin(make_request(origin="https://evil.example"))

    assert error.value.status_code == 403


def test_cross_origin_referer_is_rejected():
    with pytest.raises(HTTPException) as error:
        require_safe_origin(make_request(referer="https://evil.example/attack"))

    assert error.value.status_code == 403


def test_non_browser_client_without_origin_is_allowed():
    require_safe_origin(make_request())


def test_safe_get_does_not_require_origin():
    require_safe_origin(
        make_request(
            method="GET",
            origin="https://evil.example",
        )
    )
