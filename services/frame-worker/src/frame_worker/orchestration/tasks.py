import logging
import random
import time
from uuid import UUID

from celery.exceptions import MaxRetriesExceededError, Reject, SoftTimeLimitExceeded

from frame_worker.orchestration.celery_app import celery_app, settings
from frame_worker.orchestration.exports import (
    ExportLeaseBusy,
    PermanentExportError,
    TransientExportError,
    create_export,
    fail_unleased_export,
)
from frame_worker.orchestration.failures import classify_failure
from frame_worker.orchestration.repository import JobRepository
from frame_worker.orchestration.runner import (
    JobRunner,
    RetryableExecutionError,
    RetryLaterError,
)

logger = logging.getLogger(__name__)


class TerminalTaskError(RuntimeError):
    """Safe, serializable failure emitted after the retry budget is exhausted."""


def build_runner() -> JobRunner:
    repository = JobRepository.from_url(settings.database_url)
    return JobRunner(
        settings,
        repository,
        lambda: JobRepository.from_url(settings.database_url),
    )


@celery_app.task(
    bind=True,
    name="frame_worker.process_video",
    max_retries=2,
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def process_video(self, job_id: str) -> None:
    try:
        parsed_job_id = UUID(job_id)
    except (TypeError, ValueError) as error:
        raise Reject("Invalid job identifier", requeue=False) from error

    runner = build_runner()
    try:
        try:
            runner.execute(parsed_job_id)
        except RetryLaterError:
            # Covers the small publish-before-DB-commit window without consuming
            # the processing retry budget.
            for _ in range(5):
                time.sleep(1)
                try:
                    runner.execute(parsed_job_id)
                    return
                except RetryLaterError:
                    continue
            raise Reject(
                "Job dispatch transaction is not visible", requeue=True
            ) from None
        except RetryableExecutionError as error:
            countdown = min(300, (2**self.request.retries) * 5)
            countdown += random.uniform(0, countdown / 2)
            if self.request.retries < self.max_retries:
                raise self.retry(exc=error, countdown=countdown) from error
            try:
                raise self.retry(countdown=countdown) from error
            except MaxRetriesExceededError as exhausted:
                runner.repository.fail_queued(
                    parsed_job_id,
                    error.failure.code,
                    error.failure.message,
                )
                terminal = TerminalTaskError(error.failure.code)
                raise terminal from exhausted
    finally:
        runner.repository.close()


@celery_app.task(
    bind=True,
    name="frame_worker.process_image_dataset",
    max_retries=2,
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
    soft_time_limit=settings.dataset_soft_time_limit_seconds,
    time_limit=settings.dataset_hard_time_limit_seconds,
)
def process_image_dataset(self, job_id: str) -> None:
    try:
        parsed_job_id = UUID(job_id)
    except (TypeError, ValueError) as error:
        raise Reject("Invalid job identifier", requeue=False) from error
    runner = build_runner()
    try:
        try:
            runner.execute(parsed_job_id)
        except RetryLaterError as error:
            raise self.retry(exc=error, countdown=2) from error
        except RetryableExecutionError as error:
            countdown = min(60, (2**self.request.retries) * 5)
            countdown += random.uniform(0, countdown / 2)
            if self.request.retries < self.max_retries:
                raise self.retry(exc=error, countdown=countdown) from error
            try:
                raise self.retry(countdown=countdown) from error
            except MaxRetriesExceededError as exhausted:
                runner.repository.fail_queued(
                    parsed_job_id, error.failure.code, error.failure.message
                )
                raise TerminalTaskError(error.failure.code) from exhausted
        except SoftTimeLimitExceeded as timeout:
            error = RetryableExecutionError(classify_failure(timeout, "IMAGE_DATASET"))
            if self.request.retries < self.max_retries:
                raise self.retry(exc=error, countdown=5) from timeout
            try:
                raise self.retry(countdown=5) from timeout
            except MaxRetriesExceededError as exhausted:
                runner.repository.fail_queued(
                    parsed_job_id, error.failure.code, error.failure.message
                )
                raise TerminalTaskError(error.failure.code) from exhausted
    finally:
        runner.repository.close()


@celery_app.task(
    bind=True,
    name="frame_worker.create_frame_export",
    max_retries=2,
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
    soft_time_limit=settings.export_soft_time_limit_seconds,
    time_limit=settings.export_hard_time_limit_seconds,
)
def create_frame_export(self, export_id: str) -> None:
    try:
        parsed = UUID(export_id)
    except (TypeError, ValueError) as error:
        raise Reject("Invalid export identifier", requeue=False) from error
    _execute_export_task(self, parsed)


def _execute_export_task(task, export_id: UUID) -> None:
    try:
        create_export(export_id, settings)
    except PermanentExportError:
        logger.warning("Frame export permanently rejected export_id=%s", export_id)
        raise
    except ExportLeaseBusy as error:
        countdown = settings.export_lease_seconds + random.uniform(1, 5)
        logger.warning("Frame export lease wait scheduled export_id=%s", export_id)
        raise task.retry(exc=error, countdown=countdown) from error
    except TransientExportError as error:
        countdown = min(60, (2**task.request.retries) * 5)
        countdown += random.uniform(0, countdown / 2)
        logger.warning("Frame export retry scheduled export_id=%s", export_id)
        try:
            raise task.retry(countdown=countdown) from error
        except MaxRetriesExceededError as exhausted:
            fail_unleased_export(export_id, settings)
            logger.error("Frame export retry budget exhausted export_id=%s", export_id)
            raise error from exhausted
