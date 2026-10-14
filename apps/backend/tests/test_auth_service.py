import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.auth import User, UserSession
from app.security.auth import hash_password, hash_session_token, verify_password
from app.services.auth import (
    AuthService,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
)


class Session:
    def __init__(self, scalar_results=None, user=None, commit_error=None):
        self.scalar_results = iter(scalar_results or [])
        self.user = user
        self.commit_error = commit_error
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, item):
        self.added.append(item)

    async def scalar(self, _query):
        return next(self.scalar_results, None)

    async def get(self, model, identifier):
        if model is User and self.user and self.user.id == identifier:
            return self.user
        return None

    async def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self):
        self.rollbacks += 1


def sample_user(*, active=True):
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        email="user@example.com",
        password_hash=hash_password("StrongPass123!"),
        is_active=active,
        created_at=now,
        updated_at=now,
    )


def integrity_error(constraint):
    cause = Exception("duplicate")
    cause.diag = SimpleNamespace(constraint_name=constraint)
    return IntegrityError("duplicate", {}, cause)


def test_register_hashes_password_and_commits():
    session = Session()
    service = AuthService(session, timedelta(days=7))

    user = asyncio.run(service.register("user@example.com", "StrongPass123!"))

    assert user.email == "user@example.com"
    assert user.password_hash != "StrongPass123!"
    assert verify_password("StrongPass123!", user.password_hash)
    assert user.is_active is True
    assert session.added == [user]
    assert session.commits == 1


def test_register_duplicate_email_rolls_back():
    session = Session(commit_error=integrity_error("uq_users_email"))
    service = AuthService(session, timedelta(days=7))

    with pytest.raises(EmailAlreadyRegisteredError):
        asyncio.run(service.register("user@example.com", "StrongPass123!"))

    assert session.rollbacks == 1


def test_register_unrelated_integrity_error_is_not_hidden():
    error = integrity_error("unexpected_constraint")
    session = Session(commit_error=error)
    service = AuthService(session, timedelta(days=7))

    with pytest.raises(IntegrityError):
        asyncio.run(service.register("user@example.com", "StrongPass123!"))

    assert session.rollbacks == 1


def test_login_creates_hashed_session_token():
    user = sample_user()
    session = Session(scalar_results=[user])
    service = AuthService(session, timedelta(days=7))

    returned_user, token = asyncio.run(
        service.login("user@example.com", "StrongPass123!")
    )

    assert returned_user is user
    auth_session = next(
        item for item in session.added if isinstance(item, UserSession)
    )
    assert auth_session.user_id == user.id
    assert auth_session.token_hash == hash_session_token(token)
    assert auth_session.expires_at > auth_session.created_at
    assert session.commits == 1


@pytest.mark.parametrize(
    ("user", "password"),
    [
        (None, "StrongPass123!"),
        (sample_user(), "wrong-password"),
        (sample_user(active=False), "StrongPass123!"),
    ],
)
def test_login_rejects_invalid_credentials(user, password):
    session = Session(scalar_results=[user])
    service = AuthService(session, timedelta(days=7))

    with pytest.raises(InvalidCredentialsError):
        asyncio.run(service.login("user@example.com", password))

    assert not any(isinstance(item, UserSession) for item in session.added)


def test_authenticate_returns_active_session_user():
    user = sample_user()
    auth_session = UserSession(
        id=uuid4(),
        user_id=user.id,
        token_hash=hash_session_token("session-token"),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=7),
        revoked_at=None,
    )
    session = Session(scalar_results=[auth_session], user=user)
    service = AuthService(session, timedelta(days=7))

    authenticated = asyncio.run(service.authenticate("session-token"))

    assert authenticated is user


def test_authenticate_rejects_missing_session():
    service = AuthService(Session(scalar_results=[None]), timedelta(days=7))

    assert asyncio.run(service.authenticate("missing-token")) is None


def test_logout_revokes_existing_session():
    auth_session = UserSession(
        id=uuid4(),
        user_id=uuid4(),
        token_hash=hash_session_token("session-token"),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=7),
        revoked_at=None,
    )
    session = Session(scalar_results=[auth_session])
    service = AuthService(session, timedelta(days=7))

    asyncio.run(service.logout("session-token"))

    assert auth_session.revoked_at is not None
    assert session.commits == 1