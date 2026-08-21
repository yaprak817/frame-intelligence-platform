import logging
import threading
from collections.abc import Callable
from uuid import UUID

from frame_worker.orchestration.repository import JobRepository

logger = logging.getLogger(__name__)


class LeaseHeartbeat:
    def __init__(
        self,
        repository_factory: Callable[[], JobRepository],
        job_id: UUID,
        run_token: UUID,
        lease_seconds: float,
        interval_seconds: float,
    ) -> None:
        self._repository_factory = repository_factory
        self._job_id = job_id
        self._run_token = run_token
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"job-heartbeat-{str(job_id)[:8]}",
            daemon=True,
        )

    @property
    def ownership_lost(self) -> bool:
        return self._lost.is_set()

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval_seconds + 1)

    def _run(self) -> None:
        repository = self._repository_factory()
        try:
            while not self._stop.wait(self._interval_seconds):
                try:
                    owned = repository.heartbeat(
                        self._job_id, self._run_token, self._lease_seconds
                    )
                except Exception:
                    logger.exception("Job heartbeat failed job_id=%s", self._job_id)
                    continue
                if not owned:
                    self._lost.set()
                    logger.warning("Job ownership lost job_id=%s", self._job_id)
                    return
        finally:
            repository.close()
