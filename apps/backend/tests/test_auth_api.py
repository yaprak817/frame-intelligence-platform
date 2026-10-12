from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_auth_service
from app.main import app
from app.services.auth import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
)


def user(email="user@example.com"):
    return SimpleNamespace(
        id=uuid4(),
        email=email,
        created_at=datetime.now(UTC),
    )


class FakeAuthService:
    def __init__(self):
        self.user = user()
        self.register_error = None
        self.login_error = None
        self.authenticated_user = self.user
        self.register_calls = []
        self.login_calls = []
        self.authenticate_calls = []
        self.logout_calls = []

    async def register(self, email, password):
        self.register_calls.append((email, password))
        if self.register_error is not None:
            raise self.register_error
        self.user = user(email)
        return self.user

    async def login(self, email, password):
        self.login_calls.append((email, password))
        if self.login_error is not None:
            raise self.login_error
        return self.user, "opaque-session-token"

    async def authenticate(self, token):
        self.authenticate_calls.append(token)
        if not token:
            return None
        return self.authenticated_user

    async def logout(self, token):
        self.logout_calls.append(token)


@pytest.fixture
def auth_service():
    return FakeAuthService()


@pytest.fixture
def auth_client(auth_service):
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_register_creates_session_cookie(auth_client, auth_service):
    response = auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": " USER@Example.COM ",
            "password": "StrongPass123!",
        },
    )

    assert response.status_code == 201
    assert response.json()["email"] == "user@example.com"
    assert auth_service.register_calls == [
        ("user@example.com", "StrongPass123!")
    ]
    assert auth_service.login_calls == [
        ("user@example.com", "StrongPass123!")
    ]

    cookie = response.headers["set-cookie"].lower()
    assert "frame_session=opaque-session-token" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie


def test_register_duplicate_email_returns_conflict(auth_client, auth_service):
    auth_service.register_error = EmailAlreadyRegisteredError()

    response = auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": "user@example.com",
            "password": "StrongPass123!",
        },
    )

    assert response.status_code == 409
    assert "frame_session" not in response.headers.get("set-cookie", "")


def test_login_creates_session_cookie(auth_client, auth_service):
    response = auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": "user@example.com",
            "password": "StrongPass123!",
        },
    )

    assert response.status_code == 200
    assert response.json()["email"] == "user@example.com"
    assert auth_service.login_calls == [
        ("user@example.com", "StrongPass123!")
    ]
    assert "frame_session=opaque-session-token" in response.headers["set-cookie"]


def test_login_invalid_credentials_returns_unauthorized(
    auth_client,
    auth_service,
):
    auth_service.login_error = InvalidCredentialsError()

    response = auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": "user@example.com",
            "password": "wrong-password",
        },
    )

    assert response.status_code == 401
    assert "frame_session" not in response.headers.get("set-cookie", "")


def test_me_requires_valid_session(auth_client, auth_service):
    response = auth_client.get("/api/v1/auth/me")
    assert response.status_code == 401

    auth_client.cookies.set("frame_session", "valid-token")
    response = auth_client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json()["email"] == "user@example.com"
    assert auth_service.authenticate_calls[-1] == "valid-token"


def test_me_rejects_revoked_or_unknown_session(auth_client, auth_service):
    auth_service.authenticated_user = None
    auth_client.cookies.set("frame_session", "invalid-token")

    response = auth_client.get("/api/v1/auth/me")

    assert response.status_code == 401


def test_logout_revokes_and_clears_session_cookie(auth_client, auth_service):
    auth_client.cookies.set("frame_session", "valid-token")

    response = auth_client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    assert auth_service.logout_calls == ["valid-token"]

    cookie = response.headers["set-cookie"].lower()
    assert "frame_session=" in cookie
    assert "max-age=0" in cookie