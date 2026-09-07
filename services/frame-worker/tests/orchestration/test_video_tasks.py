import base64
import os
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault(
    "JOB_SOURCE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"T" * 32).decode()
)

from frame_worker.orchestration import tasks  # noqa: E402
from frame_worker.orchestration.failures import Failure  # noqa: E402
from frame_worker.orchestration.runner import RetryableExecutionError  # noqa: E402


def test_retry_exhaustion_marks_failed_and_keeps_task_failed(monkeypatch) -> None:
    error = RetryableExecutionError(
        Failure(
            "STORAGE_UNAVAILABLE",
            "Object storage is temporarily unavailable.",
            True,
        )
    )
    error.__cause__ = RuntimeError("safe upstream failure")
    repository = SimpleNamespace(failed=[], close=lambda: None)
    repository.fail_queued = lambda *values: repository.failed.append(values)
    runner = SimpleNamespace(
        execute=lambda _job_id: (_ for _ in ()).throw(error), repository=repository
    )
    monkeypatch.setattr(tasks, "build_runner", lambda: runner)

    job_id = uuid4()
    result = tasks.process_video.apply(
        kwargs={"job_id": str(job_id)},
        retries=tasks.process_video.max_retries,
        throw=False,
    )

    assert result.failed()
    assert isinstance(result.result, tasks.TerminalTaskError)
    assert str(result.result) == "STORAGE_UNAVAILABLE"
    assert repository.failed == [
        (job_id, "STORAGE_UNAVAILABLE", "Object storage is temporarily unavailable.")
    ]
