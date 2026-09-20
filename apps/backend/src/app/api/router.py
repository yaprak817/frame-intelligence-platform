from fastapi import APIRouter

from app.api.routes.annotations import router as annotations_router
from app.api.routes.brand_annotations import router as brand_annotations_router
from app.api.routes.brands import router as brands_router
from app.api.routes.health import router as health_router
from app.api.routes.jobs import router as jobs_router

api_router = APIRouter()

api_router.include_router(
    health_router,
    tags=["Health"],
)
api_router.include_router(
    brands_router,
    tags=["Brands"],
)
api_router.include_router(
    annotations_router,
    tags=["Annotations"],
)
api_router.include_router(
    brand_annotations_router,
    tags=["Brand Annotations"],
)
api_router.include_router(
    jobs_router,
    tags=["Jobs"],
)
