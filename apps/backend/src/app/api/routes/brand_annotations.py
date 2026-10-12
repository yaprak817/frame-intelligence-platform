from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUserDependency, get_annotation_service
from app.db.session import get_session
from app.models.brands import Brand
from app.schemas.annotations import AnnotationProjectResponse
from app.schemas.brands import CanonicalUUID
from app.services.annotations import (
    AnnotationBrandConflict,
    AnnotationLimitExceeded,
    AnnotationNotAvailable,
    AnnotationService,
)

router = APIRouter(prefix="/brands/{brand_id}/annotations")
Service = Annotated[AnnotationService, Depends(get_annotation_service)]
Session = Annotated[AsyncSession, Depends(get_session)]


async def _require_owned_brand(
    session: AsyncSession,
    brand_id: UUID,
    owner_user_id: UUID,
) -> None:
    brand = await session.get(Brand, brand_id)
    if brand is None or brand.owner_user_id != owner_user_id:
        raise HTTPException(status_code=404, detail="Marka bulunamad?.")


def _error(error: Exception) -> HTTPException:
    if isinstance(error, AnnotationBrandConflict):
        return HTTPException(
            status_code=409,
            detail={
                "code": "ANNOTATION_BRAND_CONFLICT",
                "message": "Etiketleme alanı başka bir markaya ait.",
            },
        )
    if isinstance(error, AnnotationLimitExceeded):
        return HTTPException(
            status_code=409,
            detail={
                "code": "ANNOTATION_LIMIT_EXCEEDED",
                "message": "Marka etiketleme alanı kapasite sınırına ulaştı.",
            },
        )
    if isinstance(error, AnnotationNotAvailable):
        return HTTPException(
            status_code=409,
            detail={
                "code": "ANNOTATION_NOT_AVAILABLE",
                "message": "Bu marka için etiketlenebilir hazır kare bulunamadı.",
            },
        )
    return HTTPException(
        status_code=503,
        detail={
            "code": "ANNOTATION_STORAGE_UNAVAILABLE",
            "message": "Etiketleme alanı hazırlanamadı.",
        },
    )


@router.post(
    "",
    response_model=AnnotationProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_brand_annotation_project(
    brand_id: CanonicalUUID,
    response: Response,
    service: Service,
    session: Session,
    current_user: CurrentUserDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AnnotationProjectResponse:
    await _require_owned_brand(session, brand_id, current_user.id)

    try:
        project, created = await service.get_or_create_brand(brand_id)
        response.status_code = 201 if created else 200
        response.headers["Cache-Control"] = "no-store"
        return await service.response(project, page, page_size)
    except Exception as error:
        raise _error(error) from error


@router.get("", response_model=AnnotationProjectResponse)
async def get_brand_annotation_project(
    brand_id: CanonicalUUID,
    service: Service,
    session: Session,
    current_user: CurrentUserDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AnnotationProjectResponse:
    await _require_owned_brand(session, brand_id, current_user.id)

    try:
        project = await service.get_brand(brand_id)
        return await service.response(project, page, page_size)
    except Exception as error:
        raise _error(error) from error
