import logging
import random
import time
from uuid import UUID

from celery.exceptions import Reject

from frame_worker.orchestration.celery_app import celery_app, settings
from frame_worker.orchestration.repository import JobRepository
from frame_worker.orchestration.runner import (
    JobRunner,
    RetryableExecutionError,
    RetryLaterError,
)

logger = logging.getLogger(__name__)


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
            if self.request.retries >= self.max_retries:
                runner.repository.fail_queued(
                    parsed_job_id,
                    error.failure.code,
                    error.failure.message,
                )
                return
            countdown = min(300, (2**self.request.retries) * 5)
            countdown += random.uniform(0, countdown / 2)
            raise self.retry(exc=error, countdown=countdown) from error
    finally:
        runner.repository.close()
