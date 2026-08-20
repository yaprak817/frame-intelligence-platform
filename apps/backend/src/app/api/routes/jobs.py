from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import TypeAdapter, ValidationError

from app.api.dependencies import get_job_service
from app.domain.jobs import JobStatus, SourceType
from app.models.processing_job import ProcessingJob
from app.schemas.jobs import (
    IdempotencyKey,
    JobFailureResponse,
    JobStatusResponse,
    JobSubmissionResponse,
    URLJobRequest,
)
from app.services.job_service import (
    IdempotencyConflictError,
    JobNotFoundError,
    JobService,
)

router = APIRouter(prefix="/jobs")
JobServiceDependency = Annotated[JobService, Depends(get_job_service)]


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
    if idempotency_key_header is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key header is required",
        )
    try:
        idempotency_key = TypeAdapter(IdempotencyKey).validate_python(
            idempotency_key_header
        )
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key header is invalid",
        ) from error
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


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job(
    job_id: str,
    service: JobServiceDependency,
) -> JobStatusResponse:
    try:
        parsed_id = UUID(job_id)
        job = await service.get_job(parsed_id)
    except (ValueError, JobNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        ) from error
    return _status_response(job)


def _status_response(job: ProcessingJob) -> JobStatusResponse:
    failure = None
    if job.failure_code is not None and job.failure_message is not None:
        failure = JobFailureResponse(
            code=job.failure_code,
            message=job.failure_message,
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
        result=job.result_reference,
    )
