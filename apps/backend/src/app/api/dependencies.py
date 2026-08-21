from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.repositories.jobs import SQLAlchemyJobRepository
from app.security.source_secrets import SourceSecretCipher
from app.services.job_service import JobService
from app.storage.s3 import S3MultipartUploader

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
    )
