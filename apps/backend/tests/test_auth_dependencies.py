import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.dependencies import get_current_user


class FakeAuthService:
    def __init__(self, authenticated_user=None):
        self.authenticated_user = authenticated_user
        self.calls = []

    async def authenticate(self, token):
        self.calls.append(token)
        return self.authenticated_user


def test_current_user_requires_session_cookie():
    auth = FakeAuthService()

    with pytest.raises(HTTPException) as error:
        asyncio.run(get_current_user(auth, None))

    assert error.value.status_code == 401
    assert auth.calls == []


def test_current_user_rejects_invalid_session():
    auth = FakeAuthService()

    with pytest.raises(HTTPException) as error:
        asyncio.run(get_current_user(auth, "invalid-token"))

    assert error.value.status_code == 401
    assert auth.calls == ["invalid-token"]


def test_current_user_returns_authenticated_user():
    user = SimpleNamespace(id=uuid4(), email="user@example.com")
    auth = FakeAuthService(user)

    result = asyncio.run(get_current_user(auth, "valid-token"))

    assert result is user
    assert auth.calls == ["valid-token"]