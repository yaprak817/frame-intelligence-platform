from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, field_validator


def _normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if (
        len(email) > 320
        or email.count("@") != 1
        or any(character.isspace() for character in email)
    ):
        raise ValueError("Geçerli bir e-posta adresi girin.")

    local_part, domain = email.split("@", 1)
    if not local_part or not domain or "." not in domain:
        raise ValueError("Geçerli bir e-posta adresi girin.")

    return email


class RegisterRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("Şifre en az 8 karakter olmalıdır.")
        if len(value) > 128:
            raise ValueError("Şifre en fazla 128 karakter olabilir.")
        return value


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not value or len(value) > 128:
            raise ValueError("Geçersiz şifre.")
        return value


class UserView(BaseModel):
    id: UUID
    email: str
    created_at: datetime
