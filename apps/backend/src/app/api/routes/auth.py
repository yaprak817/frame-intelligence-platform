from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.dependencies import get_auth_service
from app.schemas.auth import LoginRequest, RegisterRequest, UserView
from app.services.auth import (
    AuthService,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
)

router = APIRouter(prefix="/auth")
AuthDependency = Annotated[AuthService, Depends(get_auth_service)]

SESSION_COOKIE_NAME = "frame_session"


def _user_view(user) -> UserView:
    return UserView(
        id=user.id,
        email=user.email,
        created_at=user.created_at,
    )


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.auth_session_ttl_seconds,
        httponly=True,
        secure=settings.environment in {"staging", "production"},
        samesite="lax",
        path="/",
    )


@router.post(
    "/register",
    response_model=UserView,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    auth: AuthDependency,
) -> UserView:
    try:
        await auth.register(payload.email, payload.password)
    except EmailAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bu e-posta adresi zaten kayıtlı.",
        ) from exc

    user, token = await auth.login(payload.email, payload.password)
    _set_session_cookie(response, token, request)
    return _user_view(user)


@router.post("/login", response_model=UserView)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth: AuthDependency,
) -> UserView:
    try:
        user, token = await auth.login(payload.email, payload.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="E-posta veya şifre hatalı.",
        ) from exc

    _set_session_cookie(response, token, request)
    return _user_view(user)


@router.get("/me", response_model=UserView)
async def me(
    request: Request,
    auth: AuthDependency,
) -> UserView:
    user = await auth.authenticate(request.cookies.get(SESSION_COOKIE_NAME))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Oturum bulunamadı.",
        )
    return _user_view(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    auth: AuthDependency,
) -> None:
    await auth.logout(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=request.app.state.settings.environment in {"staging", "production"},
        samesite="lax",
    )
