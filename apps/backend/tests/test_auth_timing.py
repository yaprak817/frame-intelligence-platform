import asyncio

import pytest

import app.services.auth as auth_module
from app.services.auth import AuthService, InvalidCredentialsError


class EmptySession:
    async def scalar(self, _query):
        return None


def test_unknown_email_still_runs_password_verification(monkeypatch):
    calls = []

    def fake_verify(password, password_hash):
        calls.append((password, password_hash))
        return False

    monkeypatch.setattr(
        auth_module,
        "verify_password",
        fake_verify,
    )

    service = AuthService.__new__(AuthService)
    service.session = EmptySession()

    with pytest.raises(InvalidCredentialsError):
        asyncio.run(
            service.login(
                "missing@example.com",
                "wrong-password",
            )
        )

    assert len(calls) == 1
    assert calls[0][0] == "wrong-password"
    assert calls[0][1].startswith("$argon2")
