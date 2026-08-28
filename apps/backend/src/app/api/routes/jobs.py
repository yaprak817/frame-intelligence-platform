from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Path,
    Response,
    UploadFile,
    status,
)
from pydantic import TypeAdapter, ValidationError

from app.api.dependencies import (
    authorize_result_access,
    get_job_service,
    get_result_artifact_service,
)
from app.domain.jobs import JobStatus, SourceType
from app.models.processing_job import ProcessingJob
from app.schemas.artifacts import FrameAccessResponse, PublicResultManifest
from app.schemas.jobs import (
    IdempotencyKey,
    JobFailureResponse,
    JobResultDescriptor,
    JobStatusResponse,
    JobSubmissionResponse,
    ProcessingConfigRequest,
    URLJobRequest,
)
from app.services.job_service import (
    IdempotencyConflictError,
    JobNotFoundError,
    JobService,
    UnsupportedUploadError,
)
from app.services.result_artifacts import (
    ArtifactNotFoundError,
    FailedResultUnavailableError,
    ManifestInvalidError,
    ResultArtifactService,
    ResultJobNotFoundError,
    ResultNotReadyError,
    ResultUnavailableError,
)
from app.storage.s3 import ObjectStorageError, UploadTooLargeError

router = APIRouter(prefix="/jobs")
JobServiceDependency = Annotated[JobService, Depends(get_job_service)]
ResultServiceDependency = Annotated[
    ResultArtifactService, Depends(get_result_artifact_service)
]
ResultAuthorizationDependency = Annotated[None, Depends(authorize_result_access)]


def _idempotency_key(value: str | None) -> str:
    if value is None:
        raise HTTPException(
            status_code=422, detail="Idempotency-Key header is required"
        )
    try:
        return TypeAdapter(IdempotencyKey).validate_python(value)
    except ValidationError as error:
        raise HTTPException(
            status_code=422, detail="Idempotency-Key header is invalid"
        ) from error


@router.post(
    "/url",
    response_model=JobSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_url_job(
    request: URLJobRequest,
    response: Response,
    service: JobServiceDependency,
    idempotency_key_header: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> JobSubmissionResponse:
    idempotency_key = _idempotency_key(idempotency_key_header)
    try:
        job, _created = await service.create_url_job(
            request.url, request.processing, idempotency_key
        )
    except IdempotencyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used for a different request",
        ) from error

    status_url = f"/api/v1/jobs/{job.id}"
    response.headers["Location"] = status_url
    return JobSubmissionResponse(
        job_id=job.id,
        status=JobStatus(job.status),
        status_url=status_url,
    )


@router.post(
    "/upload",
    response_model=JobSubmissionResponse,
    status_code=202,
)
async def create_upload_job(
    response: Response,
    service: JobServiceDependency,
    file: Annotated[UploadFile, File()],
    candidate_fps: Annotated[float, Form(gt=0, le=60)] = 5.0,
    selection_window_seconds: Annotated[float, Form(gt=0, le=3600)] = 1.0,
    idempotency_key_header: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> JobSubmissionResponse:
    key = _idempotency_key(idempotency_key_header)
    try:
        job, _created = await service.create_upload_job(
            file,
            file.filename or "video",
            file.content_type or "application/octet-stream",
            file.size,
            ProcessingConfigRequest(
                candidate_fps=candidate_fps,
                selection_window_seconds=selection_window_seconds,
            ),
            key,
        )
    except UnsupportedUploadError as error:
        raise HTTPException(status_code=415, detail=str(error)) from error
    except UploadTooLargeError as error:
        raise HTTPException(
            status_code=413, detail="Uploaded video is too large"
        ) from error
    except ObjectStorageError as error:
        raise HTTPException(
            status_code=503, detail="Object storage is unavailable"
        ) from error
    except IdempotencyConflictError as error:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used for a different request",
        ) from error
    status_url = f"/api/v1/jobs/{job.id}"
    response.headers["Location"] = status_url
    return JobSubmissionResponse(
        job_id=job.id, status=JobStatus(job.status), status_url=status_url
    )


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job(
    job_id: str,
    service: JobServiceDependency,
) -> JobStatusResponse:
    try:
        parsed_id = UUID(job_id)
        job = await service.get_job(parsed_id)
    except (ValueError, JobNotFoundError) as error:
        raise _api_error(404, "JOB_NOT_FOUND", "Job not found") from error
    return _status_response(job)


def _status_response(job: ProcessingJob) -> JobStatusResponse:
    failure = None
    if job.failure_code is not None and job.failure_message is not None:
        failure = JobFailureResponse(
            code=job.failure_code,
            message=job.failure_message,
        )
    result = None
    if JobStatus(job.status) is JobStatus.SUCCEEDED:
        base_url = f"/api/v1/jobs/{job.id}/result"
        result = JobResultDescriptor(
            available=True,
            metadata_url=base_url,
            manifest_download_url=f"{base_url}/manifest",
        )
    return JobStatusResponse(
        id=job.id,
        status=JobStatus(job.status),
        source_type=SourceType(job.source_type),
        source=job.source_display,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        failure=failure,
        result=result,
    )


@router.get(
    "/{job_id}/result",
    response_model=PublicResultManifest,
)
async def get_job_result(
    job_id: str,
    service: ResultServiceDependency,
    _authorization: ResultAuthorizationDependency,
) -> PublicResultManifest:
    parsed_id = _result_job_id(job_id)
    try:
        return await service.public_manifest(parsed_id)
    except Exception as error:
        _raise_result_error(error)
        raise


@router.post(
    "/{job_id}/result/frames/{frame_index}/access",
    response_model=FrameAccessResponse,
)
async def create_frame_access(
    job_id: str,
    frame_index: Annotated[int, Path(ge=0)],
    response: Response,
    service: ResultServiceDependency,
    _authorization: ResultAuthorizationDependency,
) -> FrameAccessResponse:
    parsed_id = _result_job_id(job_id)
    try:
        result = await service.frame_access(parsed_id, frame_index)
    except Exception as error:
        _raise_result_error(error)
        raise
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return result


@router.get("/{job_id}/result/manifest")
async def download_job_manifest(
    job_id: str,
    service: ResultServiceDependency,
    _authorization: ResultAuthorizationDependency,
) -> Response:
    parsed_id = _result_job_id(job_id)
    try:
        manifest = await service.public_manifest(parsed_id)
    except Exception as error:
        _raise_result_error(error)
        raise
    return Response(
        content=manifest.model_dump_json(),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="job-{parsed_id}-manifest.json"'
            ),
            "Cache-Control": "no-store",
        },
    )


def _result_job_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise _api_error(404, "JOB_NOT_FOUND", "Job not found") from error


def _raise_result_error(error: Exception) -> None:
    if isinstance(error, ResultJobNotFoundError):
        raise _api_error(404, "JOB_NOT_FOUND", "Job not found") from error
    if isinstance(error, ResultNotReadyError):
        raise _api_error(409, "RESULT_NOT_READY", "Result is not ready") from error
    if isinstance(error, FailedResultUnavailableError):
        raise _api_error(409, "RESULT_UNAVAILABLE", "Result is unavailable") from error
    if isinstance(error, ResultUnavailableError):
        raise _api_error(502, "RESULT_UNAVAILABLE", "Result is unavailable") from error
    if isinstance(error, ManifestInvalidError):
        raise _api_error(
            502, "MANIFEST_INVALID", "Result manifest is invalid"
        ) from error
    if isinstance(error, ArtifactNotFoundError):
        raise _api_error(404, "ARTIFACT_NOT_FOUND", "Artifact not found") from error
    if isinstance(error, ObjectStorageError):
        raise _api_error(
            503, "STORAGE_UNAVAILABLE", "Storage is unavailable"
        ) from error


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )
