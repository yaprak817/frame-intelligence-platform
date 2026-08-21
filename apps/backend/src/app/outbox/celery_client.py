from typing import Protocol
from uuid import UUID

from celery import Celery

PROCESS_VIDEO_TASK = "frame_worker.process_video"


class JobMessagePublisher(Protocol):
    def publish(self, job_id: UUID) -> None: ...


class CeleryJobMessagePublisher:
    def __init__(self, broker_url: str) -> None:
        self._app = Celery("frame-intelligence-publisher", broker=broker_url)
        self._app.conf.update(
            task_serializer="json",
            accept_content=["json"],
            result_backend=None,
            task_ignore_result=True,
        )

    def publish(self, job_id: UUID) -> None:
        self._app.send_task(
            PROCESS_VIDEO_TASK,
            kwargs={"job_id": str(job_id)},
            queue="video-processing",
        )

    def close(self) -> None:
        self._app.close()
