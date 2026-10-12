from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User, UserSession
from app.security.auth import (
    generate_session_token,
    hash_password,
    hash_session_token,
    password_needs_rehash,
    verify_password,
)


class EmailAlreadyRegisteredError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class AuthService:
    def __init__(self, session: AsyncSession, session_ttl: timedelta) -> None:
        self.session = session
        self.session_ttl = session_ttl

    async def register(self, email: str, password: str) -> User:
        now = datetime.now(timezone.utc)
        user = User(
            id=uuid4(),
            email=email,
            password_hash=hash_password(password),
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        self.session.add(user)

        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            constraint_name = getattr(
                getattr(exc.orig, "diag", None),
                "constraint_name",
                None,
            )
            if constraint_name == "uq_users_email":
                raise EmailAlreadyRegisteredError from exc
            raise

        return user

    async def login(self, email: str, password: str) -> tuple[User, str]:
        user = await self.session.scalar(select(User).where(User.email == email))

        if (
            user is None
            or not user.is_active
            or not verify_password(password, user.password_hash)
        ):
            raise InvalidCredentialsError

        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
            user.updated_at = datetime.now(timezone.utc)

        token = generate_session_token()
        now = datetime.now(timezone.utc)

        self.session.add(
            UserSession(
                id=uuid4(),
                user_id=user.id,
                token_hash=hash_session_token(token),
                created_at=now,
                expires_at=now + self.session_ttl,
                revoked_at=None,
            )
        )
        await self.session.commit()

        return user, token

    async def authenticate(self, token: str | None) -> User | None:
        if not token:
            return None

        now = datetime.now(timezone.utc)
        session = await self.session.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_session_token(token),
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
        )
        if session is None:
            return None

        user = await self.session.get(User, session.user_id)
        if user is None or not user.is_active:
            return None

        return user

    async def logout(self, token: str | None) -> None:
        if not token:
            return

        session = await self.session.scalar(
            select(UserSession).where(
                UserSession.token_hash == hash_session_token(token),
                UserSession.revoked_at.is_(None),
            )
        )
        if session is None:
            return

        session.revoked_at = datetime.now(timezone.utc)
        await self.session.commit()
