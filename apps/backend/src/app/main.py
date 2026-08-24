from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import settings
from app.storage.s3 import S3ResultObjectStorage


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    storage = S3ResultObjectStorage(
        internal_endpoint=settings.object_storage_endpoint,
        external_endpoint=settings.object_storage_external_endpoint,
        access_key=settings.object_storage_access_key,
        secret_key=settings.object_storage_secret_key,
        bucket=settings.object_storage_bucket,
        region=settings.object_storage_region,
        addressing_style=settings.object_storage_addressing_style,
    )
    application.state.result_object_storage = storage
    try:
        yield
    finally:
        await storage.close()


app = FastAPI(
    title=settings.app_name,
    description="Backend API for intelligent video frame processing.",
    version=settings.app_version,
    lifespan=lifespan,
)

app.include_router(
    api_router,
    prefix="/api/v1",
)
