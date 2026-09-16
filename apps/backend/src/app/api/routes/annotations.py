from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from starlette.responses import StreamingResponse

from app.api.dependencies import (
    authorize_result_access,
    get_annotation_service,
    get_annotation_training_service,
)
from app.api.routes.jobs import _idempotency_key
from app.schemas.annotation_training import (
    AnnotationTrainingPage,
    AnnotationTrainingResponse,
    CreateAnnotationTrainingRequest,
    LatestAnnotationModelResponse,
)
from app.schemas.annotations import (
    AnnotationClassMutationResponse,
    AnnotationProjectResponse,
    CreateAnnotationClassRequest,
    ImageAnnotationsResponse,
    PutImageAnnotationsRequest,
    RevisionRequest,
    UpdateAnnotationClassRequest,
)
from app.services.annotation_training import (
    AnnotationTrainingAlreadyActive,
    AnnotationTrainingError,
    AnnotationTrainingIdempotencyConflict,
    AnnotationTrainingInsufficientImages,
    AnnotationTrainingInvalidDataset,
    AnnotationTrainingInvalidState,
    AnnotationTrainingNotFound,
    AnnotationTrainingRevisionAlreadySnapshotted,
    AnnotationTrainingRevisionConflict,
    AnnotationTrainingService,
    AnnotationTrainingSnapshotLimitReached,
    AnnotationTrainingSourceChanged,
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
TrainingService = Annotated[
    AnnotationTrainingService, Depends(get_annotation_training_service)
]
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


def training_api_error(error: Exception) -> HTTPException:
    mapping: list[tuple[type[Exception], int, str, str]] = [
        (
            AnnotationTrainingAlreadyActive,
            409,
            "ANNOTATION_TRAINING_ALREADY_ACTIVE",
            "Another annotation training is active",
        ),
        (
            AnnotationTrainingInvalidState,
            409,
            "ANNOTATION_TRAINING_INVALID_STATE",
            "Annotation training cannot be started",
        ),
        (
            AnnotationTrainingSnapshotLimitReached,
            409,
            "ANNOTATION_TRAINING_SNAPSHOT_LIMIT_REACHED",
            "Annotation training snapshot limit was reached",
        ),
        (
            AnnotationTrainingRevisionAlreadySnapshotted,
            409,
            "ANNOTATION_TRAINING_REVISION_ALREADY_SNAPSHOTTED",
            "This annotation revision already has a training snapshot",
        ),
        (
            AnnotationTrainingRevisionConflict,
            409,
            "ANNOTATION_REVISION_CONFLICT",
            "Annotation was changed by another request",
        ),
        (
            AnnotationTrainingIdempotencyConflict,
            409,
            "ANNOTATION_TRAINING_IDEMPOTENCY_CONFLICT",
            "Idempotency-Key was already used for a different request",
        ),
        (
            AnnotationTrainingInsufficientImages,
            422,
            "ANNOTATION_TRAINING_INSUFFICIENT_IMAGES",
            "At least 50 completed images are required",
        ),
        (
            AnnotationTrainingInvalidDataset,
            422,
            "ANNOTATION_TRAINING_INVALID_DATASET",
            "Annotation snapshot is not eligible for training",
        ),
        (
            AnnotationTrainingSourceChanged,
            409,
            "ANNOTATION_SOURCE_CHANGED",
            "Annotation source has changed",
        ),
        (
            AnnotationTrainingNotFound,
            404,
            "ANNOTATION_TRAINING_NOT_FOUND",
            "Annotation training was not found",
        ),
    ]
    for kind, code, public_code, message in mapping:
        if isinstance(error, kind):
            return HTTPException(code, detail={"code": public_code, "message": message})
    if isinstance(error, AnnotationTrainingError):
        return HTTPException(
            422,
            detail={
                "code": error.code,
                "message": "Annotation training is not available",
            },
        )
    return HTTPException(
        503,
        detail={
            "code": "ANNOTATION_TRAINING_UNAVAILABLE",
            "message": "Annotation training service is unavailable",
        },
    )


def _canonical_path_uuid(raw_value: str) -> UUID:
    try:
        value = UUID(raw_value)
    except (ValueError, AttributeError) as error:
        raise HTTPException(422, detail="Invalid canonical UUID") from error
    if str(value) != raw_value:
        raise HTTPException(422, detail="Invalid canonical UUID")
    return value


def _bounded_query_integer(
    raw_value: str | None, *, default: int | None, minimum: int, maximum: int
) -> int | None:
    if raw_value is None:
        return default
    if not raw_value.isascii() or not raw_value.isdigit():
        raise HTTPException(422, detail="Invalid pagination value")
    value = int(raw_value)
    if not minimum <= value <= maximum:
        raise HTTPException(422, detail="Invalid pagination value")
    return value


@router.post(
    "/trainings",
    response_model=AnnotationTrainingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_training(
    job_id: str,
    body: CreateAnnotationTrainingRequest,
    response: Response,
    service: TrainingService,
    _authorization: Authorization,
    idempotency_key_header: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> AnnotationTrainingResponse:
    parsed_job_id = _canonical_path_uuid(job_id)
    key = _idempotency_key(idempotency_key_header)
    try:
        item, created = await service.create(parsed_job_id, body, key)
    except Exception as error:
        raise training_api_error(error) from error
    response.status_code = 201 if created else 200
    response.headers["Cache-Control"] = "no-store"
    return item


@router.get("/trainings", response_model=AnnotationTrainingPage)
async def list_trainings(
    job_id: str,
    service: TrainingService,
    _authorization: Authorization,
    limit: Annotated[str | None, Query()] = None,
    after_snapshot_version: Annotated[str | None, Query()] = None,
) -> AnnotationTrainingPage:
    parsed_job_id = _canonical_path_uuid(job_id)
    parsed_limit = _bounded_query_integer(limit, default=20, minimum=1, maximum=50)
    parsed_cursor = _bounded_query_integer(
        after_snapshot_version, default=None, minimum=0, maximum=2**63 - 1
    )
    assert parsed_limit is not None
    try:
        return await service.list_runs(
            parsed_job_id,
            limit=parsed_limit,
            after_snapshot_version=parsed_cursor,
        )
    except Exception as error:
        raise training_api_error(error) from error


@router.get("/trainings/{training_id}", response_model=AnnotationTrainingResponse)
async def get_training(
    job_id: str,
    training_id: str,
    service: TrainingService,
    _authorization: Authorization,
) -> AnnotationTrainingResponse:
    parsed_job_id = _canonical_path_uuid(job_id)
    parsed_training_id = _canonical_path_uuid(training_id)
    try:
        return await service.get(parsed_job_id, parsed_training_id)
    except Exception as error:
        raise training_api_error(error) from error


@router.post(
    "/trainings/{training_id}/start", response_model=AnnotationTrainingResponse
)
async def start_training(
    job_id: str,
    training_id: str,
    response: Response,
    service: TrainingService,
    _authorization: Authorization,
) -> AnnotationTrainingResponse:
    parsed_job_id = _canonical_path_uuid(job_id)
    parsed_training_id = _canonical_path_uuid(training_id)
    try:
        item, started = await service.start(parsed_job_id, parsed_training_id)
    except Exception as error:
        raise training_api_error(error) from error
    response.status_code = 202 if started else 200
    response.headers["Cache-Control"] = "no-store"
    return item


@router.get("/models/latest", response_model=LatestAnnotationModelResponse)
async def latest_model(
    job_id: str, service: TrainingService, _authorization: Authorization
) -> LatestAnnotationModelResponse:
    parsed_job_id = _canonical_path_uuid(job_id)
    try:
        item = await service.latest_model(parsed_job_id)
    except Exception as error:
        raise training_api_error(error) from error
    assert item.model_version is not None
    return LatestAnnotationModelResponse(
        training_id=item.id,
        model_version=item.model_version,
        snapshot_version=item.snapshot_version,
        created_at=item.completed_at or item.created_at,
    )


@router.get("/trainings/{training_id}/snapshot/download")
async def download_training_snapshot(
    job_id: str,
    training_id: str,
    service: TrainingService,
    _authorization: Authorization,
) -> StreamingResponse:
    parsed_job_id = _canonical_path_uuid(job_id)
    parsed_training_id = _canonical_path_uuid(training_id)
    try:
        stream, filename = await service.snapshot_stream(
            parsed_job_id, parsed_training_id
        )
    except Exception as error:
        raise training_api_error(error) from error
    return StreamingResponse(
        stream.chunks(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(stream.metadata.size_bytes),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
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
