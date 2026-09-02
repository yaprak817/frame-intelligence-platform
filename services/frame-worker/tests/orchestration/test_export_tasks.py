import base64
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from celery.exceptions import MaxRetriesExceededError

os.environ.setdefault(
    "JOB_SOURCE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"T" * 32).decode()
)

from frame_worker.orchestration import tasks  # noqa: E402
from frame_worker.orchestration.exports import (  # noqa: E402
    ExportLeaseBusy,
    PermanentExportError,
    TransientExportError,
)


class RetrySent(RuntimeError):
    pass


class FakeTask:
    max_retries = 2

    def __init__(self, retries: int) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.retry_calls = []

    def retry(self, **kwargs):
        self.retry_calls.append(kwargs)
        if self.request.retries >= self.max_retries:
            raise MaxRetriesExceededError
        raise RetrySent


def test_transient_failure_retries_then_succeeds(monkeypatch) -> None:
    attempts = iter([TransientExportError("safe"), None])

    def execute(*_args):
        result = next(attempts)
        if result:
            raise result

    monkeypatch.setattr(tasks, "create_export", execute)
    task = FakeTask(0)
    with pytest.raises(RetrySent):
        tasks._execute_export_task(task, uuid4())
    assert task.retry_calls and task.retry_calls[0]["countdown"] <= 7.5
    tasks._execute_export_task(FakeTask(1), uuid4())


def test_retry_exhaustion_marks_failed_and_remains_a_task_failure(monkeypatch) -> None:
    export_id = uuid4()
    failed = []
    monkeypatch.setattr(
        tasks,
        "create_export",
        lambda *_args: (_ for _ in ()).throw(TransientExportError("safe")),
    )
    monkeypatch.setattr(
        tasks, "fail_unleased_export", lambda value, _settings: failed.append(value)
    )
    with pytest.raises(TransientExportError):
        tasks._execute_export_task(FakeTask(2), export_id)
    assert failed == [export_id]


def test_permanent_integrity_failure_does_not_retry(monkeypatch) -> None:
    task = FakeTask(0)
    monkeypatch.setattr(
        tasks,
        "create_export",
        lambda *_args: (_ for _ in ()).throw(PermanentExportError("safe")),
    )
    with pytest.raises(PermanentExportError):
        tasks._execute_export_task(task, uuid4())
    assert task.retry_calls == []


def test_active_lease_retry_is_bounded(monkeypatch) -> None:
    task = FakeTask(0)
    monkeypatch.setattr(
        tasks,
        "create_export",
        lambda *_args: (_ for _ in ()).throw(ExportLeaseBusy("safe")),
    )
    monkeypatch.setattr(tasks.random, "uniform", lambda *_args: 5)
    with pytest.raises(RetrySent):
        tasks._execute_export_task(task, uuid4())
    assert task.retry_calls[0]["countdown"] == (tasks.settings.export_lease_seconds + 5)


def test_real_celery_retry_exhaustion_marks_failed_and_is_not_success(
    monkeypatch,
) -> None:
    export_id = uuid4()
    failed = []
    monkeypatch.setattr(
        tasks,
        "create_export",
        lambda *_args: (_ for _ in ()).throw(TransientExportError("safe")),
    )
    monkeypatch.setattr(
        tasks, "fail_unleased_export", lambda value, _settings: failed.append(value)
    )
    monkeypatch.setattr(tasks.random, "uniform", lambda *_args: 0)
    result = tasks.create_frame_export.apply(
        kwargs={"export_id": str(export_id)}, throw=False
    )
    assert result.failed()
    assert isinstance(result.result, TransientExportError)
    assert failed == [export_id]


def test_export_task_uses_bounded_soft_and_hard_limits() -> None:
    assert tasks.create_frame_export.soft_time_limit == (
        tasks.settings.export_soft_time_limit_seconds
    )
    assert tasks.create_frame_export.time_limit == (
        tasks.settings.export_hard_time_limit_seconds
    )
    assert 0 < tasks.create_frame_export.soft_time_limit
    assert (
        tasks.create_frame_export.soft_time_limit < tasks.create_frame_export.time_limit
    )
