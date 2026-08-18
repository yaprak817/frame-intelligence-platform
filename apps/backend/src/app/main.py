from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import settings

app = FastAPI(
    title=settings.app_name,
    description="Backend API for intelligent video frame processing.",
    version=settings.app_version,
)

app.include_router(
    api_router,
    prefix="/api/v1",
)
