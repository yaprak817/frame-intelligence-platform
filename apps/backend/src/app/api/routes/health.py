import asyncio

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db.session import engine

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": "frame-intelligence-api",
    }


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "healthy"}


@router.get("/ready", response_model=None)
async def readiness(request: Request) -> dict[str, str] | JSONResponse:
    timeout = request.app.state.settings.dependency_timeout_seconds

    async def database_ready() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    checks = (
        database_ready(),
        request.app.state.redis.ping(),
        request.app.state.result_object_storage.ready(),
    )
    try:
        await asyncio.wait_for(asyncio.gather(*checks), timeout=timeout)
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable"},
        )
    return {"status": "ready"}
