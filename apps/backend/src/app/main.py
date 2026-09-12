from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.api.router import api_router
from app.core.config import settings
from app.core.observability import RequestLoggingMiddleware, configure_logging
from app.security.rate_limit import RateLimitMiddleware, RedisRateLimiter
from app.storage.s3 import S3ResultObjectStorage


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.environment in {"staging", "production"})
    storage = S3ResultObjectStorage(
        internal_endpoint=settings.object_storage_endpoint,
        external_endpoint=settings.object_storage_external_endpoint,
        access_key=settings.object_storage_access_key,
        secret_key=settings.object_storage_secret_key,
        bucket=settings.object_storage_bucket,
        region=settings.object_storage_region,
        addressing_style=settings.object_storage_addressing_style,
        dependency_timeout_seconds=settings.dependency_timeout_seconds,
    )
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    application.state.result_object_storage = storage
    application.state.redis = redis
    application.state.rate_limiter = RedisRateLimiter(redis)
    application.state.settings = settings
    try:
        yield
    finally:
        await redis.aclose()
        await storage.close()


app = FastAPI(
    title=settings.app_name,
    description="Backend API for intelligent video frame processing.",
    version=settings.app_version,
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def safe_request_validation_error(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    if "/annotations" not in request.url.path:
        return await request_validation_exception_handler(request, error)
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
            }
        },
    )


app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLoggingMiddleware)
if settings.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(
    api_router,
    prefix="/api/v1",
)
