from datetime import timedelta
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.models.auth import User
from app.repositories.jobs import SQLAlchemyJobRepository
from app.security.source_secrets import SourceSecretCipher
from app.services.annotation_inference import AnnotationInferenceService
from app.services.annotation_training import AnnotationTrainingService
from app.services.annotations import AnnotationService
from app.services.auth import AuthService
from app.services.frame_exports import FrameExportService
from app.services.job_service import JobNotFoundError, JobService
from app.services.result_artifacts import ResultArtifactService
from app.storage.s3 import S3MultipartUploader, S3ResultObjectStorage

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def get_auth_service(session: SessionDependency) -> AuthService:
    return AuthService(
        session,
        timedelta(seconds=settings.auth_session_ttl_seconds),
    )


def get_job_service(session: SessionDependency) -> JobService:
    return JobService(
        SQLAlchemyJobRepository(session),
        SourceSecretCipher(settings.job_source_encryption_key),
        S3MultipartUploader(
            endpoint=settings.object_storage_endpoint,
            access_key=settings.object_storage_access_key,
            secret_key=settings.object_storage_secret_key,
            bucket=settings.object_storage_bucket,
            region=settings.object_storage_region,
            addressing_style=settings.object_storage_addressing_style,
            max_bytes=settings.max_upload_bytes,
            chunk_bytes=settings.object_storage_multipart_chunk_bytes,
        ),
        image_max_files=settings.image_dataset_max_files,
        image_max_file_bytes=settings.image_dataset_max_file_bytes,
        image_max_total_bytes=settings.image_dataset_max_total_bytes,
        image_zip_max_compressed_bytes=(
            settings.image_dataset_zip_max_compressed_bytes
        ),
    )


def get_result_artifact_service(
    request: Request, session: SessionDependency
) -> ResultArtifactService:
    storage: S3ResultObjectStorage = request.app.state.result_object_storage
    return ResultArtifactService(
        SQLAlchemyJobRepository(session),
        storage,
        settings.result_artifact_url_ttl_seconds,
        settings.image_dataset_max_file_bytes,
        settings.image_dataset_max_total_bytes,
        settings.result_artifact_spool_min_free_bytes,
    )


def get_frame_export_service(
    request: Request, session: SessionDependency
) -> FrameExportService:
    return FrameExportService(
        session,
        get_result_artifact_service(request, session),
        max_frames=settings.frame_export_max_frames,
        max_total_bytes=settings.frame_export_max_total_bytes,
    )


def get_annotation_service(
    request: Request, session: SessionDependency
) -> AnnotationService:
    return AnnotationService(session, get_result_artifact_service(request, session))


def get_annotation_training_service(
    request: Request, session: SessionDependency
) -> AnnotationTrainingService:
    return AnnotationTrainingService(
        session,
        get_result_artifact_service(request, session),
        max_snapshots_per_project=settings.annotation_training_max_snapshots_per_project,
    )


def get_annotation_inference_service(
    request: Request, session: SessionDependency
) -> AnnotationInferenceService:
    return AnnotationInferenceService(
        session,
        get_result_artifact_service(request, session),
    )


SESSION_COOKIE_NAME = "frame_session"


_SAFE_ORIGIN_METHODS = {"GET", "HEAD", "OPTIONS"}


def _canonical_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None

    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"

    default_port = (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    )

    origin = f"{scheme}://{host}"
    if port is not None and not default_port:
        origin += f":{port}"
    return origin


def require_safe_origin(request: Request) -> None:
    if request.method.upper() in _SAFE_ORIGIN_METHODS:
        return

    supplied = request.headers.get("origin")
    if supplied is None:
        supplied = request.headers.get("referer")

    # Non-browser/API clients may legitimately omit both headers.
    if supplied is None:
        return

    supplied_origin = _canonical_origin(supplied)
    request_origin = _canonical_origin(str(request.base_url))

    allowed_origins = {
        origin
        for origin in (request_origin,)
        if origin is not None
    }

    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        for configured in settings.cors_allow_origins:
            normalized = _canonical_origin(configured)
            if normalized is not None:
                allowed_origins.add(normalized)

    if supplied_origin is None or supplied_origin not in allowed_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "CROSS_ORIGIN_REQUEST_REJECTED",
                "message": "Cross-origin request rejected",
            },
        )


async def get_current_user(
    auth: Annotated[AuthService, Depends(get_auth_service)],
    session_token: Annotated[
        str | None,
        Cookie(alias=SESSION_COOKIE_NAME),
    ] = None,
) -> User:
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    user = await auth.authenticate(session_token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    return user


CurrentUserDependency = Annotated[User, Depends(get_current_user)]

async def authorize_result_access(
    request: Request,
    current_user: CurrentUserDependency,
    service: Annotated[JobService, Depends(get_job_service)],
) -> None:
    raw_job_id = request.path_params.get("job_id")
    try:
        job_id = UUID(str(raw_job_id))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "JOB_NOT_FOUND", "message": "Job not found"},
        ) from None

    try:
        job = await service.get_job(job_id)
    except JobNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "JOB_NOT_FOUND", "message": "Job not found"},
        ) from None

    if job.owner_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "JOB_NOT_FOUND", "message": "Job not found"},
        )
