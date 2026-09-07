from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.repositories.jobs import SQLAlchemyJobRepository
from app.security.source_secrets import SourceSecretCipher
from app.services.frame_exports import FrameExportService
from app.services.job_service import JobService
from app.services.result_artifacts import ResultArtifactService
from app.storage.s3 import S3MultipartUploader, S3ResultObjectStorage

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


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


async def authorize_result_access() -> None:
    """Single-tenant authorization seam for future ownership enforcement."""
