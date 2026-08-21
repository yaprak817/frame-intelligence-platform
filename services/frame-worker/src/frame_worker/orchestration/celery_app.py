from celery import Celery

from frame_worker.orchestration.config import WorkerSettings

settings = WorkerSettings.from_env()
celery_app = Celery(
    "frame-intelligence-worker",
    broker=settings.celery_broker_url,
    include=["frame_worker.orchestration.tasks"],
)
celery_app.conf.update(
    result_backend=None,
    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    enable_utc=True,
    timezone="UTC",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=False,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "visibility_timeout": settings.visibility_timeout_seconds,
    },
    worker_concurrency=settings.worker_concurrency,
    task_routes={"frame_worker.process_video": {"queue": "video-processing"}},
)
