from typing import Protocol
from uuid import UUID

from celery import Celery

PROCESS_VIDEO_TASK = "frame_worker.process_video"
PROCESS_IMAGE_DATASET_TASK = "frame_worker.process_image_dataset"
CREATE_FRAME_EXPORT_TASK = "frame_worker.create_frame_export"


class JobMessagePublisher(Protocol):
    def publish(self, job_id: UUID) -> None: ...
    def publish_image_dataset(self, job_id: UUID) -> None: ...
    def publish_export(self, export_id: UUID) -> None: ...


class CeleryJobMessagePublisher:
    def __init__(
        self,
        broker_url: str,
        task_queue: str = "video-processing",
        redis_keyprefix: str = "",
    ) -> None:
        self._app = Celery("frame-intelligence-publisher", broker=broker_url)
        self._task_queue = task_queue
        self._app.conf.update(
            task_serializer="json",
            accept_content=["json"],
            result_backend=None,
            task_ignore_result=True,
            broker_transport_options={"global_keyprefix": redis_keyprefix},
        )

    def verify_connection(self) -> None:
        with self._app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=0)

    def publish(self, job_id: UUID) -> None:
        self._app.send_task(
            PROCESS_VIDEO_TASK,
            kwargs={"job_id": str(job_id)},
            queue=self._task_queue,
        )

    def publish_image_dataset(self, job_id: UUID) -> None:
        self._app.send_task(
            PROCESS_IMAGE_DATASET_TASK,
            kwargs={"job_id": str(job_id)},
            queue=self._task_queue,
        )

    def publish_export(self, export_id: UUID) -> None:
        self._app.send_task(
            CREATE_FRAME_EXPORT_TASK,
            kwargs={"export_id": str(export_id)},
            queue=self._task_queue,
        )

    def close(self) -> None:
        self._app.close()
