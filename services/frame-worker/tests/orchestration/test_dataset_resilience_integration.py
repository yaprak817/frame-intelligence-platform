import io
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import boto3
import httpx
import numpy as np
import psycopg
import pytest
from PIL import Image

os.environ.setdefault(
    "JOB_SOURCE_ENCRYPTION_KEY", "VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ="
)

from celery.exceptions import SoftTimeLimitExceeded

from frame_worker.datasets import processor as dataset_processor
from frame_worker.orchestration import runner as runner_module
from frame_worker.orchestration import tasks
from frame_worker.orchestration.celery_app import celery_app, settings
from frame_worker.orchestration.repository import JobRepository
from frame_worker.orchestration.runner import JobRunner, RetryableExecutionError

pytestmark = pytest.mark.skipif(
    os.environ.get("ASYNC_E2E_INTEGRATION") != "1",
    reason="ASYNC_E2E_INTEGRATION=1 is required",
)


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=settings.object_storage_endpoint,
        aws_access_key_id=settings.object_storage_access_key,
        aws_secret_access_key=settings.object_storage_secret_key,
        region_name=settings.object_storage_region,
    )


def _inventory(prefix: str) -> set[str]:
    response = _s3().list_objects_v2(
        Bucket=settings.object_storage_bucket, Prefix=prefix
    )
    return {item["Key"] for item in response.get("Contents", [])}


def _wait_for(predicate, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise TimeoutError("Bounded integration wait expired")


def _images(count: int) -> list[tuple[str, tuple[str, bytes, str]]]:
    files = []
    for index in range(count):
        pixels = np.random.default_rng(index).integers(
            0, 256, size=(320, 320, 3), dtype=np.uint8
        )
        body = io.BytesIO()
        Image.fromarray(pixels).save(body, format="JPEG", quality=95)
        files.append(
            ("files", (f"image-{index:03d}.jpg", body.getvalue(), "image/jpeg"))
        )
    return files


@contextmanager
def _worker(name: str):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "frame_worker.orchestration.celery_app:celery_app",
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--loglevel=WARNING",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--queues",
            settings.task_queue,
            "--hostname",
            f"{name}@localhost",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(lambda: process.poll() is None, 5)
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@contextmanager
def _instrumented_worker(name: str, queue: str, bootstrap: str):
    process = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(lambda: process.poll() is None, 5)
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def _worker_bootstrap(
    name: str,
    queue: str,
    received: Path,
    completed: Path,
    claimed: Path | None = None,
    release: Path | None = None,
    invocation: Path | None = None,
) -> str:
    barrier = ""
    if invocation:
        wait = ""
        if claimed and release:
            wait = f"""
    Path({str(claimed)!r}).write_text("claimed", encoding="ascii")
    deadline = time.monotonic() + 30
    while not Path({str(release)!r}).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("dataset barrier timed out")
        time.sleep(0.05)
"""
        barrier = f"""
from frame_worker.orchestration import runner as runner_module
original_process_dataset = runner_module.process_dataset
def blocked_process_dataset(*args, **kwargs):
    with Path({str(invocation)!r}).open("a", encoding="ascii") as stream:
        stream.write({name!r} + "\\n")
{wait}
    return original_process_dataset(*args, **kwargs)
runner_module.process_dataset = blocked_process_dataset
"""
    return f"""
import json
import time
from pathlib import Path
from celery import signals
{barrier}
@signals.task_prerun.connect(weak=False)
def record_received(task_id=None, task=None, **kwargs):
    if task and task.name == "frame_worker.process_image_dataset":
        Path({str(received)!r}).write_text(str(task_id), encoding="ascii")
@signals.task_postrun.connect(weak=False)
def record_completed(task_id=None, task=None, state=None, **kwargs):
    if task and task.name == "frame_worker.process_image_dataset":
        result = {{"task_id": str(task_id), "state": state}}
        Path({str(completed)!r}).write_text(
            json.dumps(result), encoding="ascii"
        )
from frame_worker.orchestration.celery_app import celery_app
celery_app.worker_main([
    "worker", "--pool=solo", "--concurrency=1", "--loglevel=WARNING",
    "--without-gossip", "--without-mingle", "--without-heartbeat",
    "--queues", {queue!r}, "--hostname", {name!r} + "@localhost",
])
"""


def _row(database_url: str, job_id: UUID):
    with psycopg.connect(database_url) as connection:
        return connection.execute(
            "SELECT status, attempt_count, run_token, result_reference, "
            "lease_expires_at "
            "FROM processing_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()


def test_real_worker_hard_loss_redelivery_cleans_only_stale_attempt() -> None:
    database_url = settings.database_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    backend_url = os.environ["BACKEND_URL"]
    other_job, previous = uuid4(), uuid4()
    client = _s3()
    job_id = None
    preservation: set[str] = set()
    with _worker("dataset-hard-loss-a") as worker_a:
        response = httpx.post(
            f"{backend_url}/api/v1/jobs/image-dataset",
            headers={"Idempotency-Key": f"hard-loss-{uuid4()}"},
            files=_images(60),
            timeout=60,
        )
        response.raise_for_status()
        job_id = UUID(response.json()["job_id"])
        preservation = {
            f"jobs/{job_id}/results/{previous}/manifest.json",
            f"jobs/{other_job}/results/{previous}/foreign.tmp",
        }
        for key in preservation:
            client.put_object(
                Bucket=settings.object_storage_bucket, Key=key, Body=b"preserve"
            )

        def running_row():
            row = _row(database_url, job_id)
            return row if row and row[0] == "RUNNING" else None

        running = _wait_for(running_row, 30)
        stale_token = running[2]
        stale_prefix = f"jobs/{job_id}/results/{stale_token}/"
        partial = _wait_for(
            lambda: next(
                (
                    key
                    for key in _inventory(stale_prefix)
                    if not key.endswith("manifest.json")
                ),
                None,
            ),
            60,
        )
        assert partial
        worker_a.kill()
        worker_a.wait(timeout=10)

    def expired_row():
        row = _row(database_url, job_id)
        return row if row and row[4] and row[4] <= datetime.now(UTC) else None

    _wait_for(expired_row, settings.lease_seconds + 5)
    with _worker("dataset-hard-loss-b"):
        celery_app.send_task(
            "frame_worker.process_image_dataset",
            kwargs={"job_id": str(job_id)},
            queue=settings.task_queue,
        )

        def succeeded_row():
            row = _row(database_url, job_id)
            return row if row and row[0] == "SUCCEEDED" else None

        finished = _wait_for(succeeded_row, 90)

    assert finished[1] == 2
    manifest_key = finished[3].removeprefix(f"s3://{settings.object_storage_bucket}/")
    final_prefix = manifest_key.removesuffix("manifest.json")
    assert _inventory(stale_prefix) == set()
    assert manifest_key in _inventory(final_prefix)
    assert preservation.issubset(
        _inventory(f"jobs/{job_id}/") | _inventory(f"jobs/{other_job}/")
    )
    assert (
        len({key for key in _inventory(final_prefix) if key.endswith("/manifest.json")})
        == 1
    )

    repository = JobRepository.from_url(settings.database_url)
    try:
        assert repository.heartbeat(job_id, stale_token, 60) is False
        assert repository.fail(job_id, stale_token, "FAILED", "safe") is False
        assert repository.succeed(job_id, stale_token, {}, "stale") is False
    finally:
        repository.close()
        for prefix in (f"jobs/{job_id}/", f"jobs/{other_job}/"):
            for key in _inventory(prefix):
                client.delete_object(Bucket=settings.object_storage_bucket, Key=key)
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))


def test_two_real_workers_concurrent_duplicate_delivery_publishes_once(
    tmp_path,
) -> None:
    database_url = settings.database_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    backend_url = os.environ["BACKEND_URL"]
    client = _s3()
    job_id = None
    foreign_job = uuid4()
    foreign_key = f"jobs/{foreign_job}/results/{uuid4()}/foreign.tmp"
    claimed = tmp_path / "worker-a-claimed"
    release = tmp_path / "worker-a-release"
    invocation = tmp_path / "processor-invocation"
    a_received = tmp_path / "worker-a-received"
    a_completed = tmp_path / "worker-a-completed"
    b_received = tmp_path / "worker-b-received"
    b_completed = tmp_path / "worker-b-completed"
    queue_b = f"{settings.task_queue}-duplicate"
    client.put_object(
        Bucket=settings.object_storage_bucket, Key=foreign_key, Body=b"preserve"
    )
    try:
        a_bootstrap = _worker_bootstrap(
            "dataset-duplicate-a",
            settings.task_queue,
            a_received,
            a_completed,
            claimed,
            release,
            invocation,
        )
        with _instrumented_worker(
            "dataset-duplicate-a", settings.task_queue, a_bootstrap
        ):
            response = httpx.post(
                f"{backend_url}/api/v1/jobs/image-dataset",
                headers={"Idempotency-Key": f"concurrent-duplicate-{uuid4()}"},
                files=_images(40),
                timeout=60,
            )
            response.raise_for_status()
            job_id = UUID(response.json()["job_id"])
            _wait_for(lambda: claimed.exists(), 30)
            running = _row(database_url, job_id)
            assert running[0] == "RUNNING"
            assert running[1] == 1
            active_token = running[2]

            def dispatched():
                with psycopg.connect(database_url) as connection:
                    return connection.execute(
                        "SELECT published_at, attempt_count FROM job_outbox "
                        "WHERE aggregate_id=%s",
                        (job_id,),
                    ).fetchone()

            outbox = _wait_for(
                lambda: row if (row := dispatched()) and row[0] else None, 15
            )
            assert outbox[1] == 1
            b_bootstrap = _worker_bootstrap(
                "dataset-duplicate-b",
                queue_b,
                b_received,
                b_completed,
                invocation=invocation,
            )
            with _instrumented_worker("dataset-duplicate-b", queue_b, b_bootstrap):
                duplicate = celery_app.send_task(
                    "frame_worker.process_image_dataset",
                    kwargs={"job_id": str(job_id)},
                    queue=queue_b,
                )
                _wait_for(lambda: b_received.exists(), 15)
                _wait_for(lambda: b_completed.exists(), 15)
                b_result = json.loads(b_completed.read_text(encoding="ascii"))
                assert b_received.read_text(encoding="ascii") == duplicate.id
                assert b_result == {"task_id": duplicate.id, "state": "SUCCESS"}
                assert _row(database_url, job_id)[0:3] == (
                    "RUNNING",
                    1,
                    active_token,
                )
                assert invocation.read_text(encoding="ascii").splitlines() == [
                    "dataset-duplicate-a"
                ]

            release.write_text("release", encoding="ascii")

            def succeeded_row():
                row = _row(database_url, job_id)
                return row if row and row[0] == "SUCCEEDED" else None

            finished = _wait_for(succeeded_row, 90)
            _wait_for(lambda: a_completed.exists(), 15)

        assert finished[1] == 1
        a_result = json.loads(a_completed.read_text(encoding="ascii"))
        assert a_result["state"] == "SUCCESS"
        assert a_result["task_id"] == a_received.read_text(encoding="ascii")
        assert a_result["task_id"] != b_result["task_id"]
        manifest_key = finished[3].removeprefix(
            f"s3://{settings.object_storage_bucket}/"
        )
        inventory = _inventory(f"jobs/{job_id}/")
        assert manifest_key in inventory
        assert sum(key.endswith("/manifest.json") for key in inventory) == 1
        assert foreign_key in _inventory(f"jobs/{foreign_job}/")
        with psycopg.connect(database_url) as connection:
            outbox = connection.execute(
                "SELECT published_at, attempt_count FROM job_outbox "
                "WHERE aggregate_id=%s",
                (job_id,),
            ).fetchone()
        assert outbox[0] is not None
        assert outbox[1] == 1
        manifest = json.loads(
            client.get_object(Bucket=settings.object_storage_bucket, Key=manifest_key)[
                "Body"
            ].read()
        )
        expected = {manifest_key}
        expected.update(
            value
            for item in manifest["images"]
            for value in (item.get("object_key"), item.get("yolo_object_key"))
            if value
        )
        expected.update(item["object_key"] for item in manifest["exports"].values())
        with psycopg.connect(database_url) as connection:
            source = connection.execute(
                "SELECT source_reference FROM processing_jobs WHERE id=%s", (job_id,)
            ).fetchone()[0]
        expected.update(item["object_key"] for item in source["items"])
        assert inventory == expected
    finally:
        for prefix in filter(
            None, (f"jobs/{job_id}/" if job_id else None, f"jobs/{foreign_job}/")
        ):
            for key in _inventory(prefix):
                client.delete_object(Bucket=settings.object_storage_bucket, Key=key)
        if job_id:
            with psycopg.connect(database_url) as connection:
                connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))


def test_real_minio_soft_timeout_cleans_temp_partial_and_body(monkeypatch) -> None:
    database_url = settings.database_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    response = httpx.post(
        f"{os.environ['BACKEND_URL']}/api/v1/jobs/image-dataset",
        headers={"Idempotency-Key": f"soft-timeout-{uuid4()}"},
        files=_images(1),
        timeout=30,
    )
    response.raise_for_status()
    job_id = UUID(response.json()["job_id"])
    _wait_for(lambda: (_row(database_url, job_id) or (None,))[0] == "QUEUED", 15)

    real_client = _s3()
    bodies = []
    workspaces = []

    class TimeoutClient:
        def __getattr__(self, name):
            return getattr(real_client, name)

        def upload_fileobj(self, body, bucket, key, ExtraArgs=None):
            bodies.append(body)
            workspaces.append(
                next(
                    path
                    for path in Path(body.name).parents
                    if path.name.startswith("image-dataset-")
                )
            )
            real_client.upload_fileobj(body, bucket, key, ExtraArgs=ExtraArgs)
            assert key in _inventory(f"jobs/{job_id}/")
            raise SoftTimeLimitExceeded

    class RetrySignal(RuntimeError):
        pass

    repository = JobRepository.from_url(settings.database_url)
    runner = JobRunner(
        settings, repository, lambda: JobRepository.from_url(settings.database_url)
    )
    monkeypatch.setattr(dataset_processor, "_client", lambda _settings: TimeoutClient())
    monkeypatch.setattr(
        runner_module, "process_dataset", dataset_processor.process_dataset
    )
    monkeypatch.setattr(tasks, "build_runner", lambda: runner)
    monkeypatch.setattr(
        tasks.process_image_dataset,
        "retry",
        lambda **kwargs: (_ for _ in ()).throw(RetrySignal(kwargs["exc"])),
    )
    try:
        with pytest.raises(RetrySignal) as captured:
            tasks.process_image_dataset.run(str(job_id))
        assert isinstance(captured.value.args[0], RetryableExecutionError)
        assert bodies and all(body.closed for body in bodies)
        assert workspaces and all(not path.exists() for path in workspaces)
        assert _inventory(f"jobs/{job_id}/results/") == set()
        row = _row(database_url, job_id)
        assert row[0] == "QUEUED"
        assert row[3] is None
    finally:
        for key in _inventory(f"jobs/{job_id}/"):
            real_client.delete_object(Bucket=settings.object_storage_bucket, Key=key)
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))
