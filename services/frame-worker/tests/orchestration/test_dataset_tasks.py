import base64
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded

os.environ.setdefault(
    "JOB_SOURCE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"T" * 32).decode()
)

from frame_worker.artifacts.object_storage import PersistedArtifacts  # noqa: E402
from frame_worker.datasets.processor import DatasetOutput  # noqa: E402
from frame_worker.orchestration import runner as runner_module  # noqa: E402
from frame_worker.orchestration import tasks  # noqa: E402
from frame_worker.orchestration.contracts import (  # noqa: E402
    ClaimResult,
    JobRecord,
    SourceType,
)
from frame_worker.orchestration.failures import Failure, classify_failure  # noqa: E402
from frame_worker.orchestration.runner import (  # noqa: E402
    JobRunner,
    RetryableExecutionError,
)


class RetrySent(RuntimeError):
    pass


class Repository:
    def __init__(self) -> None:
        self.failed = []
        self.closed = False

    def fail_queued(self, *values) -> None:
        self.failed.append(values)

    def close(self) -> None:
        self.closed = True


def runner(error=None):
    repository = Repository()

    def execute(_job_id):
        if error is not None:
            raise error
        return False

    return SimpleNamespace(execute=execute, repository=repository)


def transient() -> RetryableExecutionError:
    return RetryableExecutionError(Failure("STORAGE_UNAVAILABLE", "safe", True))


def test_dataset_transient_failure_uses_bounded_retry(monkeypatch) -> None:
    instance = runner(transient())
    monkeypatch.setattr(tasks, "build_runner", lambda: instance)
    monkeypatch.setattr(tasks.random, "uniform", lambda *_args: 0)

    def retry(**kwargs):
        assert 0 < kwargs["countdown"] <= 60
        assert isinstance(kwargs["exc"], RetryableExecutionError)
        raise RetrySent

    monkeypatch.setattr(tasks.process_image_dataset, "retry", retry)
    with pytest.raises(RetrySent):
        tasks.process_image_dataset.run(str(uuid4()))
    assert instance.repository.failed == []
    assert instance.repository.closed


def test_dataset_retry_exhaustion_fails_and_preserves_error(monkeypatch) -> None:
    error = transient()
    instance = runner(error)
    monkeypatch.setattr(tasks, "build_runner", lambda: instance)
    monkeypatch.setattr(
        tasks.process_image_dataset.request,
        "retries",
        tasks.process_image_dataset.max_retries,
    )
    monkeypatch.setattr(
        tasks.process_image_dataset,
        "retry",
        lambda **_kwargs: (_ for _ in ()).throw(MaxRetriesExceededError()),
    )
    with pytest.raises(tasks.TerminalTaskError) as captured:
        tasks.process_image_dataset.run(str(uuid4()))
    assert str(captured.value) == "STORAGE_UNAVAILABLE"
    assert len(instance.repository.failed) == 1
    assert instance.repository.failed[0][1:] == ("STORAGE_UNAVAILABLE", "safe")
    assert instance.repository.closed


def test_dataset_permanent_result_does_not_retry(monkeypatch) -> None:
    instance = runner()
    monkeypatch.setattr(tasks, "build_runner", lambda: instance)
    retry = SimpleNamespace(calls=0)

    def unexpected_retry(**_kwargs):
        retry.calls += 1

    monkeypatch.setattr(tasks.process_image_dataset, "retry", unexpected_retry)
    tasks.process_image_dataset.run(str(uuid4()))
    assert retry.calls == 0
    assert instance.repository.closed


def test_dataset_soft_timeout_is_safe_retryable_failure(monkeypatch) -> None:
    failure = classify_failure(SoftTimeLimitExceeded(), "IMAGE_DATASET")
    assert failure == Failure(
        "DATASET_TIMEOUT", "The image dataset processing timed out.", True
    )
    instance = runner(SoftTimeLimitExceeded())
    monkeypatch.setattr(tasks, "build_runner", lambda: instance)
    monkeypatch.setattr(
        tasks.process_image_dataset,
        "retry",
        lambda **kwargs: (_ for _ in ()).throw(RetrySent(kwargs["exc"])),
    )
    with pytest.raises(RetrySent):
        tasks.process_image_dataset.run(str(uuid4()))
    assert instance.repository.closed


def test_dataset_duplicate_redelivery_runs_wrapper_without_double_publish(
    monkeypatch,
) -> None:
    calls = []
    instance = runner()
    instance.execute = lambda job_id: calls.append(job_id) or len(calls) == 1
    monkeypatch.setattr(tasks, "build_runner", lambda: instance)
    job_id = uuid4()
    tasks.process_image_dataset.run(str(job_id))
    tasks.process_image_dataset.run(str(job_id))
    assert calls == [job_id, job_id]
    assert tasks.process_image_dataset.acks_late
    assert tasks.process_image_dataset.reject_on_worker_lost


def test_dataset_task_limits_and_retry_budget_are_bound_to_config() -> None:
    assert tasks.process_image_dataset.max_retries == 2
    assert tasks.process_image_dataset.soft_time_limit == (
        tasks.settings.dataset_soft_time_limit_seconds
    )
    assert tasks.process_image_dataset.time_limit == (
        tasks.settings.dataset_hard_time_limit_seconds
    )
    assert 0 < tasks.process_image_dataset.soft_time_limit
    assert (
        tasks.process_image_dataset.soft_time_limit
        < tasks.process_image_dataset.time_limit
    )


class WorkerHardLoss(BaseException):
    pass


class NoopHeartbeat:
    ownership_lost = False

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def dataset_job(job_id, token, attempt):
    return JobRecord(
        id=job_id,
        status="RUNNING",
        source_type=SourceType.IMAGE_DATASET,
        source_secret=None,
        source_reference={"schema_version": 1, "kind": "images", "items": [{}]},
        processing_config={"dataset_schema_version": 1},
        attempt_count=attempt,
        run_token=token,
        lease_expires_at=None,
        result_reference=None,
        result_summary=None,
        version=1,
    )


def test_dataset_task_hard_loss_redelivery_and_token_fencing(
    monkeypatch, tmp_path
) -> None:
    job_id, stale, current, previous, other_job = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    stale_prefix = f"jobs/{job_id}/results/{stale}/"
    current_prefix = f"jobs/{job_id}/results/{current}/"
    inventory = {
        stale_prefix + "images/partial.jpg",
        current_prefix + "active.tmp",
        f"jobs/{job_id}/results/{previous}/manifest.json",
        f"jobs/{other_job}/results/{stale}/foreign.tmp",
    }

    class StatefulRepository:
        def __init__(self) -> None:
            self.claims = 0
            self.owner = stale
            self.publishes = []

        def claim(self, claimed_id, _lease):
            assert claimed_id == job_id
            self.claims += 1
            if self.claims == 1:
                return ClaimResult(dataset_job(job_id, stale, 1))
            self.owner = current
            return ClaimResult(dataset_job(job_id, current, 2), stale_run_token=stale)

        def heartbeat(self, _job_id, token, _lease):
            return token == self.owner

        def succeed(self, _job_id, token, summary, reference):
            if token != self.owner:
                return False
            self.publishes.append((token, summary, reference))
            return True

        def fail(self, _job_id, token, *_args):
            return token == self.owner

        def close(self):
            pass

    repository = StatefulRepository()
    worker_settings = replace(tasks.settings, processing_temp_root=tmp_path)
    job_runner = JobRunner(worker_settings, repository, lambda: repository)
    executions = 0

    def process(_job, _workspace, _settings):
        nonlocal executions
        executions += 1
        if executions == 1:
            raise WorkerHardLoss
        manifest = current_prefix + "manifest.json"
        inventory.add(manifest)
        return DatasetOutput(
            {"accepted_files": 1},
            PersistedArtifacts(
                f"s3://test/{manifest}", current_prefix.rstrip("/"), (manifest,)
            ),
        )

    def cleanup(_settings, cleaned_job, stale_token, current_token):
        assert (cleaned_job, stale_token, current_token) == (
            job_id,
            stale,
            current,
        )
        inventory.difference_update(
            key for key in tuple(inventory) if key.startswith(stale_prefix)
        )

    monkeypatch.setattr(runner_module, "LeaseHeartbeat", NoopHeartbeat)
    monkeypatch.setattr(runner_module, "process_dataset", process)
    monkeypatch.setattr(runner_module, "cleanup_stale_run", cleanup)
    monkeypatch.setattr(tasks, "build_runner", lambda: job_runner)

    with pytest.raises(WorkerHardLoss):
        tasks.process_image_dataset.run(str(job_id))
    tasks.process_image_dataset.run(str(job_id))

    assert repository.heartbeat(job_id, stale, 60) is False
    assert repository.fail(job_id, stale, "FAILED", "safe") is False
    assert repository.succeed(job_id, stale, {}, "stale") is False
    assert [publish[0] for publish in repository.publishes] == [current]
    assert stale_prefix + "images/partial.jpg" not in inventory
    assert current_prefix + "active.tmp" in inventory
    assert current_prefix + "manifest.json" in inventory
    assert f"jobs/{job_id}/results/{previous}/manifest.json" in inventory
    assert f"jobs/{other_job}/results/{stale}/foreign.tmp" in inventory
    assert sum(key.endswith("/manifest.json") for key in inventory) == 2


def test_dataset_task_duplicate_delivery_publishes_once(monkeypatch) -> None:
    job_id = uuid4()
    calls = 0
    publishes = []

    def execute(_job_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            publishes.append("manifest")
            return True
        return False

    instance = runner()
    instance.execute = execute
    monkeypatch.setattr(tasks, "build_runner", lambda: instance)
    tasks.process_image_dataset.run(str(job_id))
    tasks.process_image_dataset.run(str(job_id))
    assert calls == 2
    assert publishes == ["manifest"]


def test_dataset_task_cleanup_failure_retries_and_preserves_error(
    monkeypatch,
) -> None:
    cleanup_failure = RetryableExecutionError(
        Failure("STORAGE_UNAVAILABLE", "safe", True)
    )
    instance = runner(cleanup_failure)
    monkeypatch.setattr(tasks, "build_runner", lambda: instance)
    monkeypatch.setattr(
        tasks.process_image_dataset,
        "retry",
        lambda **kwargs: (_ for _ in ()).throw(RetrySent(kwargs["exc"])),
    )
    with pytest.raises(RetrySent) as captured:
        tasks.process_image_dataset.run(str(uuid4()))
    assert captured.value.args[0] is cleanup_failure
