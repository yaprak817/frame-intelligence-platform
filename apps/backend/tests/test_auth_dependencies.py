import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.dependencies import authorize_result_access, get_current_user


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


class OwnershipService:
    def __init__(self, job=None):
        self.job = job

    async def get_job(self, identifier):
        if self.job is not None and self.job.id == identifier:
            return self.job
        return None


def ownership_request(job_id):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/v1/jobs/{job_id}/result",
            "headers": [],
            "path_params": {"job_id": str(job_id)},
        }
    )


def test_result_access_allows_job_owner():
    user = SimpleNamespace(id=uuid4())
    job = SimpleNamespace(id=uuid4(), owner_user_id=user.id)

    asyncio.run(
        authorize_result_access(
            ownership_request(job.id),
            user,
            OwnershipService(job),
        )
    )


def test_result_access_hides_other_users_job():
    user = SimpleNamespace(id=uuid4())
    job = SimpleNamespace(id=uuid4(), owner_user_id=uuid4())

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            authorize_result_access(
                ownership_request(job.id),
                user,
                OwnershipService(job),
            )
        )

    assert error.value.status_code == 404
