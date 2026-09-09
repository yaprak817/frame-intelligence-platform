from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from starlette.responses import StreamingResponse

from app.api.dependencies import authorize_result_access, get_annotation_service
from app.schemas.annotations import (
    AnnotationClassMutationResponse,
    AnnotationProjectResponse,
    CreateAnnotationClassRequest,
    ImageAnnotationsResponse,
    PutImageAnnotationsRequest,
    RevisionRequest,
    UpdateAnnotationClassRequest,
)
from app.services.annotations import (
    AnnotationClassInUse,
    AnnotationClassInvalid,
    AnnotationClassOrderLocked,
    AnnotationImageNotFound,
    AnnotationLimitExceeded,
    AnnotationNotAvailable,
    AnnotationRevisionConflict,
    AnnotationService,
    AnnotationSourceChanged,
)
from app.services.result_artifacts import (
    FailedResultUnavailableError,
    ManifestInvalidError,
    ResultJobNotFoundError,
    ResultNotReadyError,
    ResultUnavailableError,
)
from app.storage.s3 import ObjectStorageError

router = APIRouter(prefix="/jobs/{job_id}/annotations")
Service = Annotated[AnnotationService, Depends(get_annotation_service)]
Authorization = Annotated[None, Depends(authorize_result_access)]


def api_error(error: Exception) -> HTTPException:
    mapping: list[tuple[type[Exception], int, str, str]] = [
        (
            AnnotationRevisionConflict,
            409,
            "ANNOTATION_REVISION_CONFLICT",
            "Annotation was changed by another request",
        ),
        (
            AnnotationClassInUse,
            409,
            "ANNOTATION_CLASS_IN_USE",
            "Annotation class is in use",
        ),
        (
            AnnotationClassOrderLocked,
            409,
            "ANNOTATION_CLASS_ORDER_LOCKED",
            "Annotation class order is locked",
        ),
        (
            AnnotationClassInvalid,
            422,
            "ANNOTATION_CLASS_INVALID",
            "Annotation class is invalid",
        ),
        (
            AnnotationLimitExceeded,
            422,
            "ANNOTATION_LIMIT_EXCEEDED",
            "Annotation limit was exceeded",
        ),
        (
            AnnotationImageNotFound,
            404,
            "ANNOTATION_IMAGE_NOT_FOUND",
            "Annotation image was not found",
        ),
        (
            AnnotationSourceChanged,
            409,
            "ANNOTATION_SOURCE_CHANGED",
            "Annotation source has changed",
        ),
        (
            AnnotationNotAvailable,
            409,
            "ANNOTATION_NOT_AVAILABLE",
            "Annotations are not available",
        ),
        (
            (ResultJobNotFoundError, ResultNotReadyError, FailedResultUnavailableError),
            409,
            "ANNOTATION_NOT_AVAILABLE",
            "Annotations are not available",
        ),
        (
            ResultUnavailableError,
            409,
            "ANNOTATION_SOURCE_CHANGED",
            "Annotation source has changed",
        ),
        (
            ManifestInvalidError,
            409,
            "ANNOTATION_SOURCE_CHANGED",
            "Annotation source has changed",
        ),
        (
            ObjectStorageError,
            503,
            "ANNOTATION_STORAGE_UNAVAILABLE",
            "Annotation storage is unavailable",
        ),
    ]
    for kind, code, public_code, message in mapping:
        if isinstance(error, kind):
            return HTTPException(code, detail={"code": public_code, "message": message})
    return HTTPException(
        503,
        detail={
            "code": "ANNOTATION_STORAGE_UNAVAILABLE",
            "message": "Annotation service is unavailable",
        },
    )


async def project(service: AnnotationService, job_id: UUID):
    try:
        return await service.get(job_id)
    except Exception as error:
        raise api_error(error) from error


@router.post(
    "", response_model=AnnotationProjectResponse, status_code=status.HTTP_201_CREATED
)
async def create_project(
    job_id: UUID,
    request: Request,
    response: Response,
    service: Service,
    _authorization: Authorization,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AnnotationProjectResponse:
    if await request.body():
        raise HTTPException(
            422,
            detail={
                "code": "ANNOTATION_NOT_AVAILABLE",
                "message": "Annotation project request must not include a body",
            },
        )
    try:
        item, created = await service.get_or_create(job_id)
        response.status_code = 201 if created else 200
        response.headers["Cache-Control"] = "no-store"
        return await service.response(item, page, page_size)
    except Exception as error:
        raise api_error(error) from error


@router.get("", response_model=AnnotationProjectResponse)
async def get_project(
    job_id: UUID,
    service: Service,
    _authorization: Authorization,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AnnotationProjectResponse:
    item = await project(service, job_id)
    return await service.response(item, page, page_size)


@router.post("/classes", response_model=AnnotationClassMutationResponse)
async def create_class(
    job_id: UUID,
    request: CreateAnnotationClassRequest,
    service: Service,
    _authorization: Authorization,
) -> AnnotationClassMutationResponse:
    try:
        return await service.create_class(await service.get(job_id), request)
    except Exception as error:
        raise api_error(error) from error


@router.patch("/classes/{class_id}", response_model=AnnotationClassMutationResponse)
async def update_class(
    job_id: UUID,
    class_id: UUID,
    request: UpdateAnnotationClassRequest,
    service: Service,
    _authorization: Authorization,
) -> AnnotationClassMutationResponse:
    try:
        return await service.update_class(await service.get(job_id), class_id, request)
    except Exception as error:
        raise api_error(error) from error


@router.delete("/classes/{class_id}", response_model=AnnotationClassMutationResponse)
async def delete_class(
    job_id: UUID,
    class_id: UUID,
    request: RevisionRequest,
    service: Service,
    _authorization: Authorization,
) -> AnnotationClassMutationResponse:
    try:
        return await service.delete_class(
            await service.get(job_id), class_id, request.expected_revision
        )
    except Exception as error:
        raise api_error(error) from error


@router.get("/images/{image_index}", response_model=ImageAnnotationsResponse)
async def get_image(
    job_id: UUID,
    image_index: Annotated[int, Path(ge=0)],
    service: Service,
    _authorization: Authorization,
) -> ImageAnnotationsResponse:
    try:
        return await service.image(await service.get(job_id), image_index)
    except Exception as error:
        raise api_error(error) from error


@router.put("/images/{image_index}", response_model=ImageAnnotationsResponse)
async def put_image(
    job_id: UUID,
    image_index: Annotated[int, Path(ge=0)],
    request: PutImageAnnotationsRequest,
    service: Service,
    _authorization: Authorization,
) -> ImageAnnotationsResponse:
    try:
        return await service.put_image(await service.get(job_id), image_index, request)
    except Exception as error:
        raise api_error(error) from error


@router.get("/images/{image_index}/preview")
async def preview(
    job_id: UUID,
    image_index: Annotated[int, Path(ge=0)],
    service: Service,
    _authorization: Authorization,
) -> StreamingResponse:
    try:
        stream = await service.preview(await service.get(job_id), image_index)
    except Exception as error:
        raise api_error(error) from error
    return StreamingResponse(
        stream.chunks(),
        media_type="image/jpeg",
        headers={
            "Content-Length": str(stream.metadata.size_bytes),
            "Content-Disposition": (
                f'inline; filename="annotation-{image_index:06d}.jpg"'
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
