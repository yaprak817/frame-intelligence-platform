import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.routes.health as health_module
from app.main import app
from app.storage.s3 import S3ResultObjectStorage

client = TestClient(app)


def test_health_check() -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "frame-intelligence-api",
    }


def test_liveness_does_not_check_dependencies() -> None:
    response = client.get("/api/v1/live")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_readiness_reports_only_general_status(monkeypatch) -> None:
    class Connection:
        async def execute(self, _query) -> None:
            return None

    class ConnectionContext:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return ConnectionContext()

    class ReadyRedis:
        async def ping(self) -> bool:
            return True

    class ReadyStorage:
        async def ready(self) -> None:
            return None

    monkeypatch.setattr(health_module, "engine", Engine())
    with TestClient(app) as readiness_client:
        readiness_client.app.state.redis = ReadyRedis()
        readiness_client.app.state.result_object_storage = ReadyStorage()
        response = readiness_client.get("/api/v1/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_failure_is_safe_503(monkeypatch) -> None:
    class FailingEngine:
        def connect(self):
            raise RuntimeError("postgresql://user:secret@internal-db:5432/db")

    monkeypatch.setattr(health_module, "engine", FailingEngine())
    with TestClient(app) as readiness_client:
        response = readiness_client.get("/api/v1/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "internal-db" not in response.text


def test_readiness_timeout_is_bounded_and_safe(monkeypatch) -> None:
    class HangingEngine:
        def connect(self):
            return ConnectionContext()

    class ConnectionContext:
        async def __aenter__(self):
            await asyncio.Event().wait()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(health_module, "engine", HangingEngine())
    with TestClient(app) as readiness_client:
        readiness_client.app.state.settings = SimpleNamespace(
            dependency_timeout_seconds=0.1
        )
        started = time.perf_counter()
        response = readiness_client.get("/api/v1/ready")
        elapsed = time.perf_counter() - started
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert elapsed < 0.5


def test_storage_readiness_probe_is_single_flight_and_reusable() -> None:
    class BlockingClient:
        def __init__(self):
            self.calls = 0
            self.started = threading.Event()
            self.release = threading.Event()

        def head_bucket(self, **_kwargs):
            self.calls += 1
            self.started.set()
            self.release.wait(1)

    async def scenario() -> None:
        client = BlockingClient()
        storage = S3ResultObjectStorage(
            internal_endpoint="http://internal",
            external_endpoint="http://external",
            access_key="fixture-access",
            secret_key="fixture-secret",
            bucket="fixture-bucket",
            region="us-east-1",
            addressing_style="path",
            internal_client=client,
            signing_client=client,
        )
        original_ready_probe_task = storage._ready_probe_task
        barrier = asyncio.Event()
        release_waiters = asyncio.Event()
        selected_tasks: list[asyncio.Task[None]] = []

        async def ready_probe_task_at_barrier() -> asyncio.Task[None]:
            task = await original_ready_probe_task()
            selected_tasks.append(task)
            if len(selected_tasks) == 2:
                barrier.set()
            await release_waiters.wait()
            return task

        storage._ready_probe_task = ready_probe_task_at_barrier
        first = asyncio.create_task(storage.ready())
        second = asyncio.create_task(storage.ready())
        await asyncio.wait_for(barrier.wait(), timeout=1)
        assert len(selected_tasks) == 2
        assert selected_tasks[0] is selected_tasks[1]
        assert await asyncio.to_thread(client.started.wait, 1)
        assert client.calls == 1
        release_waiters.set()
        client.release.set()
        await asyncio.gather(first, second)
        storage._ready_probe_task = original_ready_probe_task
        client.started.clear()
        client.release.clear()
        client.release.set()
        await storage.ready()
        assert client.calls == 2

    asyncio.run(scenario())


class ControlledS3Client:
    def __init__(self) -> None:
        self.calls = 0
        self.close_calls = 0
        self.fail = True
        self.started = threading.Event()
        self.finished = threading.Event()
        self.release = threading.Event()

    def head_bucket(self, **_kwargs) -> None:
        self.calls += 1
        self.started.set()
        self.release.wait(1)
        self.finished.set()
        if self.fail:
            raise RuntimeError("https://secret@storage.internal/fixture-bucket")

    def close(self) -> None:
        self.close_calls += 1


def controlled_storage(client: ControlledS3Client) -> S3ResultObjectStorage:
    storage = S3ResultObjectStorage(
        internal_endpoint="http://internal",
        external_endpoint="http://external",
        access_key="fixture-access",
        secret_key="fixture-secret",
        bucket="fixture-bucket",
        region="us-east-1",
        addressing_style="path",
        dependency_timeout_seconds=0.01,
        internal_client=client,
        signing_client=client,
    )
    storage._owns_internal_client = True
    return storage


def test_s3_timeout_reuses_probe_consumes_failure_and_recovers(monkeypatch) -> None:
    class Connection:
        async def execute(self, _query) -> None:
            return None

    class ConnectionContext:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return ConnectionContext()

    class ReadyRedis:
        async def ping(self) -> bool:
            return True

    monkeypatch.setattr(health_module, "engine", Engine())
    s3_client = ControlledS3Client()
    storage = controlled_storage(s3_client)
    with TestClient(app) as readiness_client:
        readiness_client.app.state.settings = SimpleNamespace(
            dependency_timeout_seconds=0.02
        )
        readiness_client.app.state.redis = ReadyRedis()
        readiness_client.app.state.result_object_storage = storage
        responses = [readiness_client.get("/api/v1/ready") for _ in range(3)]
        assert all(response.status_code == 503 for response in responses)
        assert all(
            response.json() == {"status": "unavailable"} for response in responses
        )
        assert s3_client.calls == 1
        failed_probe = storage._ready_task
        assert failed_probe is not None
        s3_client.release.set()
        assert s3_client.finished.wait(1)
        with pytest.raises(Exception, match="Object storage request failed"):
            readiness_client.portal.call(lambda: failed_probe)
        assert failed_probe.done()
        assert failed_probe.exception() is not None
        s3_client.fail = False
        response = readiness_client.get("/api/v1/ready")
        assert response.status_code == 200
        assert s3_client.calls == 2


def test_probe_exception_is_retrieved_after_caller_timeout() -> None:
    async def scenario() -> None:
        client = ControlledS3Client()
        storage = controlled_storage(client)
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(storage.ready(), timeout=0.01)
            assert await asyncio.to_thread(client.started.wait, 1)
            client.release.set()
            assert await asyncio.to_thread(client.finished.wait, 1)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert unhandled == []
        finally:
            loop.set_exception_handler(previous)
            await storage.close()

    asyncio.run(scenario())


def test_shutdown_defers_client_close_until_probe_finishes() -> None:
    async def scenario() -> None:
        client = ControlledS3Client()
        client.fail = False
        storage = controlled_storage(client)
        caller = asyncio.create_task(storage.ready())
        assert await asyncio.to_thread(client.started.wait, 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await storage.close()
        assert client.close_calls == 0
        with pytest.raises(Exception, match="unavailable"):
            await storage.ready()
        client.release.set()
        assert await asyncio.to_thread(client.finished.wait, 1)
        for _ in range(5):
            await asyncio.sleep(0)
            if storage._deferred_close_task is not None:
                break
        assert storage._deferred_close_task is not None
        await storage._deferred_close_task
        assert client.close_calls == 1
        await storage.close()
        assert client.close_calls == 1

    asyncio.run(scenario())


def test_deferred_close_exception_is_consumed_and_reference_is_cleared() -> None:
    class FailingCloseClient(ControlledS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.fail = False
            self.close_started = threading.Event()
            self.close_release = threading.Event()

        def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            self.close_release.wait(1)
            raise RuntimeError("https://secret@storage.internal/fixture-bucket")

    async def scenario() -> None:
        client = FailingCloseClient()
        storage = controlled_storage(client)
        caller = asyncio.create_task(storage.ready())
        assert await asyncio.to_thread(client.started.wait, 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await storage.close()

        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            client.release.set()
            assert await asyncio.to_thread(client.close_started.wait, 1)
            deferred = storage._deferred_close_task
            assert deferred is not None
            completed = asyncio.Event()
            deferred.add_done_callback(lambda _task: completed.set())
            client.close_release.set()
            await asyncio.wait_for(completed.wait(), timeout=1)
            assert deferred.done()
            assert storage._deferred_close_task is None
            assert client.close_calls == 1
            assert unhandled == []
        finally:
            loop.set_exception_handler(previous)

    asyncio.run(scenario())
