"""Run the async worker integration suite with task-scoped infrastructure."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol
from urllib.parse import urlsplit, urlunsplit

import boto3
import psycopg
import redis
from botocore.exceptions import ClientError

TARGET_REVISION = "20260902_0005"
CLEANUP_FAILURE_EXIT = 2
REQUIRED_CLEANUP_STEPS = (
    "PROCESS_TREE_STOP",
    "PROCESS_TREE_VERIFY",
    "REDIS_CLEANUP",
    "REDIS_VERIFY",
    "STORAGE_CLEANUP",
    "STORAGE_VERIFY",
    "DATABASE_CLEANUP",
    "DATABASE_VERIFY",
    "LOG_CLEANUP",
    "READY_FILE_CLEANUP",
    "PROCESS_REGISTRY_CLEANUP",
    "TEMP_CLEANUP",
    "TEMP_VERIFY",
)
REDACTIONS = (
    re.compile(
        r"(?i)\b(database|bucket|object_key|object_path|namespace|dsn|password|"
        r"secret|token|key|credential)\s*=\s*([^\s,;&]+)"
    ),
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s]+"),
    re.compile(r"(?i)\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|redis|s3)://[^\s]+"),
    re.compile(r"https?://[^\s]+"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"),
    re.compile(r"(?i)(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}"),
)


def _redact(value: str, sensitive_values: tuple[str, ...] = ()) -> str:
    for sensitive in sorted(filter(None, sensitive_values), key=len, reverse=True):
        value = value.replace(sensitive, "[REDACTED]")
    value = REDACTIONS[0].sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    for pattern in REDACTIONS[1:]:
        value = pattern.sub("[REDACTED]", value)
    return value[-12_000:]


def _safe_exit_code(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255
        else 1
    )


def _publish_pytest_status(exit_code: object) -> int:
    safe_exit_code = _safe_exit_code(exit_code)
    status = "PASSED" if safe_exit_code == 0 else "FAILED"
    print(f"pytest_status={status} exit_code={safe_exit_code}")
    return safe_exit_code


def _wait_for_pytest(
    process: subprocess.Popen[bytes], timeout: float, output_limit: int = 65_536
) -> tuple[int, bytes]:
    tails = [bytearray(), bytearray()]

    def drain(stream: object, tail: bytearray) -> None:
        if not hasattr(stream, "read"):
            return
        while chunk := stream.read(65_536):
            tail.extend(chunk)
            if len(tail) > output_limit:
                del tail[:-output_limit]

    readers = [
        threading.Thread(target=drain, args=(stream, tail), daemon=True)
        for stream, tail in zip((process.stdout, process.stderr), tails, strict=True)
    ]
    for reader in readers:
        reader.start()
    process.wait(timeout=timeout)
    deadline = time.monotonic() + 10
    for reader in readers:
        reader.join(timeout=max(0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        raise RuntimeError("Pytest output drain timed out")
    return _publish_pytest_status(process.returncode), b"".join(tails)


def _database_url(base: str, name: str) -> str:
    parsed = urlsplit(base.replace("postgresql+psycopg://", "postgresql://", 1))
    return urlunsplit((*parsed[:2], f"/{name}", parsed.query, parsed.fragment))


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_ready(process: subprocess.Popen[bytes], port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Managed service exited before readiness")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("Managed service readiness timed out")


def _prepare_ready_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def _wait_file_ready(
    process: subprocess.Popen[bytes], path: Path, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Managed publisher exited before readiness")
        if path.is_file() and path.read_text(encoding="ascii") == "ready":
            return
        time.sleep(0.05)
    raise TimeoutError("Managed publisher readiness timed out")


def _final_status(test_exit_code: int, cleanup_errors: list[str]) -> int:
    if cleanup_errors:
        print("cleanup_failed=" + ",".join(cleanup_errors), file=sys.stderr)
    if test_exit_code:
        return test_exit_code
    return CLEANUP_FAILURE_EXIT if cleanup_errors else 0


def _run_cleanup_steps(
    steps: list[tuple[str, Callable[[], None]]],
) -> list[str]:
    errors: list[str] = []
    for code, action in steps:
        try:
            action()
        except Exception:
            errors.append(code)
    return errors


def _publish_cleanup_receipt(path: Path | None, errors: list[str]) -> list[str]:
    if path is None:
        return errors
    try:
        if errors:
            path.unlink(missing_ok=True)
        else:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"status": "clean", "verified": list(REQUIRED_CLEANUP_STEPS)},
                    separators=(",", ":"),
                ),
                encoding="ascii",
            )
            os.replace(temporary, path)
    except Exception:
        errors.append("RECEIPT_WRITE")
    return errors


def _complete_cleanup(
    test_exit_code: int,
    steps: list[tuple[str, Callable[[], None]]],
    receipt: Path | None,
) -> int:
    errors = _run_cleanup_steps(steps)
    if tuple(code for code, _action in steps) != REQUIRED_CLEANUP_STEPS:
        errors.append("CLEANUP_CHAIN_VERIFY")
    _publish_cleanup_receipt(receipt, errors)
    return _final_status(test_exit_code, errors)


def _execute_lifecycle(
    execute: Callable[[], int],
    operations: CleanupOperations,
    receipt: Path | None,
) -> int:
    test_exit_code = 1
    failure_code: str | None = None
    result = 1
    try:
        test_exit_code = execute()
    except KeyboardInterrupt:
        failure_code = "RUN_INTERRUPTED"
    except Exception:
        failure_code = "RUN_FAILED"
    finally:
        if failure_code:
            print(f"run_failed={failure_code}", file=sys.stderr)
            if receipt is not None:
                receipt.unlink(missing_ok=True)
        result = _complete_cleanup(
            test_exit_code,
            _cleanup_steps(operations),
            None if failure_code else receipt,
        )
    return result


class CleanupOperations(Protocol):
    def __getitem__(self, code: str) -> Callable[[], None]: ...


def _cleanup_steps(
    operations: CleanupOperations,
) -> list[tuple[str, Callable[[], None]]]:
    return [(code, operations[code]) for code in REQUIRED_CLEANUP_STEPS]


class ProcessIdentity(NamedTuple):
    pid: int
    parent_pid: int
    created: str


def _same_process(left: ProcessIdentity, right: ProcessIdentity) -> bool:
    return left.pid == right.pid and left.created == right.created


def _process_snapshot() -> dict[int, ProcessIdentity]:
    if sys.platform == "win32":
        command = (
            "Get-CimInstance Win32_Process | ForEach-Object { "
            "'{0}|{1}|{2}' -f $_.ProcessId,$_.ParentProcessId,$_.CreationDate }"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        rows = completed.stdout.splitlines()
    else:
        rows = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="ascii")
                fields = stat[stat.rindex(") ") + 2 :].split()
                if fields[0] == "Z":
                    continue
                rows.append(f"{entry.name}|{fields[1]}|{fields[19]}")
            except (FileNotFoundError, PermissionError, IndexError, ValueError):
                continue
    snapshot: dict[int, ProcessIdentity] = {}
    for row in rows:
        try:
            pid_value, parent_value, created = row.strip().split("|", 2)
            identity = ProcessIdentity(int(pid_value), int(parent_value), created)
            snapshot[identity.pid] = identity
        except (TypeError, ValueError):
            continue
    return snapshot


class ProcessRegistry:
    def __init__(
        self,
        path: Path,
        *,
        windows: bool | None = None,
        windows_control_event: int | None = None,
        signal_sender: Callable[[int, int], None] = os.kill,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = path
        self._windows = sys.platform == "win32" if windows is None else windows
        self._windows_control_event = (
            getattr(signal, "CTRL_BREAK_EVENT", None)
            if windows is None and self._windows
            else windows_control_event
        )
        self._signal_sender = signal_sender
        self._monotonic = monotonic
        self._sleep = sleep
        self._owned: dict[int, ProcessIdentity] = {}
        self._roots: set[int] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._monitor: threading.Thread | None = None

    def _write(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        payload = [identity._asdict() for identity in self._owned.values()]
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")), encoding="ascii"
        )
        os.replace(temporary, self.path)

    def register_root(self, pid: int) -> None:
        identity = _process_snapshot().get(pid)
        if identity is None:
            raise RuntimeError("Process identity is unavailable")
        with self._lock:
            self._roots.add(pid)
            self._owned[pid] = identity
            self._write()

    def refresh(self) -> None:
        snapshot = _process_snapshot()
        with self._lock:
            for pid, identity in self._owned.items():
                if pid in snapshot and not _same_process(snapshot[pid], identity):
                    raise RuntimeError("Process ownership mismatch")
            owned = set(self._roots) | set(self._owned)
            changed = True
            while changed:
                changed = False
                for identity in snapshot.values():
                    if identity.pid not in owned and identity.parent_pid in owned:
                        owned.add(identity.pid)
                        changed = True
            for pid in sorted(owned):
                if pid in snapshot:
                    self._owned.setdefault(pid, snapshot[pid])
            self._write()

    def start_monitor(self) -> None:
        def monitor() -> None:
            while not self._stop_event.wait(0.1):
                try:
                    self.refresh()
                except Exception:
                    pass

        self._monitor = threading.Thread(target=monitor, daemon=True)
        self._monitor.start()

    def stop_monitor(self) -> None:
        self._stop_event.set()
        if self._monitor:
            self._monitor.join(timeout=2)
        self.refresh()

    def _matching_live(self) -> list[ProcessIdentity]:
        snapshot = _process_snapshot()
        mismatched = [
            identity
            for identity in self._owned.values()
            if identity.pid in snapshot
            and not _same_process(snapshot[identity.pid], identity)
        ]
        if mismatched:
            raise RuntimeError("Process ownership mismatch")
        return [
            identity for identity in self._owned.values() if identity.pid in snapshot
        ]

    def _depth(self, identity: ProcessIdentity) -> int:
        depth = 0
        parent = identity.parent_pid
        while parent in self._owned:
            depth += 1
            parent = self._owned[parent].parent_pid
        return depth

    def stop_all(self) -> None:
        self.stop_monitor()
        live = sorted(
            self._matching_live(),
            key=lambda item: (self._depth(item), item.pid),
            reverse=True,
        )
        graceful = (
            [identity for identity in live if identity.pid in self._roots]
            if self._windows
            else live
        )
        for identity in graceful:
            graceful_signal = (
                self._windows_control_event if self._windows else signal.SIGTERM
            )
            if graceful_signal is None:
                continue
            try:
                self._signal_sender(identity.pid, graceful_signal)
            except OSError:
                continue
        deadline = self._monotonic() + 10
        while self._monotonic() < deadline and self._matching_live():
            self._sleep(0.05)
        force_signal = signal.SIGTERM if self._windows else signal.SIGKILL
        for identity in self._matching_live():
            try:
                self._signal_sender(identity.pid, force_signal)
            except ProcessLookupError:
                continue
        deadline = self._monotonic() + 5
        while self._monotonic() < deadline and self._matching_live():
            self._sleep(0.05)
        if self._matching_live():
            raise RuntimeError("Owned process remained alive")

    def verify_empty(self) -> None:
        if self._matching_live():
            raise RuntimeError("Owned process remained alive")

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)
        self.path.with_suffix(".tmp").unlink(missing_ok=True)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        control_event = getattr(signal, "CTRL_BREAK_EVENT", None)
        if control_event is None:
            process.kill()
        else:
            process.send_signal(control_event)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main(
    *,
    cleanup_operations: CleanupOperations | None = None,
    test_exit_code: int | None = None,
    cleanup_receipt: Path | None = None,
    execute: Callable[[], int] | None = None,
) -> int:
    if cleanup_operations is not None:
        execute_operation = execute or (lambda: test_exit_code or 0)
        return _execute_lifecycle(
            execute_operation,
            cleanup_operations,
            cleanup_receipt,
        )
    parser = argparse.ArgumentParser()
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    root = Path(__file__).resolve().parents[3]
    worker = root / "services" / "frame-worker"
    backend = root / "apps" / "backend"
    run_id = secrets.token_hex(6)
    database_name = f"frame_async_{run_id}"
    bucket = f"frame-async-{run_id}"
    queue = f"frame-async-{run_id}"
    port = _free_loopback_port()
    backend_python = (
        backend
        / ".venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    admin_url = os.environ.get(
        "TEST_DATABASE_ADMIN_URL",
        os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://frame_user:change_me@localhost:5432/postgres",
        ),
    )
    admin_url = _database_url(admin_url, "postgres")
    database_url = _database_url(admin_url, database_name)
    redis_base = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    redis_parts = urlsplit(redis_base)
    redis_db = secrets.SystemRandom().choice(range(1, 14))
    broker_url = urlunsplit((*redis_parts[:2], f"/{redis_db}", "", ""))
    storage_endpoint = os.environ.get(
        "OBJECT_STORAGE_ENDPOINT", "http://localhost:9000"
    )
    access_key = os.environ.get("OBJECT_STORAGE_ACCESS_KEY", "frame_admin")
    secret_key = os.environ.get("OBJECT_STORAGE_SECRET_KEY", "change_me")
    sensitive_values = (
        database_name,
        bucket,
        queue,
        admin_url,
        database_url,
        broker_url,
        storage_endpoint,
        access_key,
        secret_key,
    )
    env = os.environ.copy()
    env.update(
        ASYNC_E2E_INTEGRATION="1",
        OBJECT_STORAGE_INTEGRATION="1",
        DATABASE_URL=database_url.replace("postgresql://", "postgresql+psycopg://", 1),
        TEST_DATABASE_URL=database_url.replace(
            "postgresql://", "postgresql+psycopg://", 1
        ),
        CELERY_BROKER_URL=broker_url,
        REDIS_URL=broker_url,
        CELERY_TASK_QUEUE=queue,
        CELERY_REDIS_KEYPREFIX=f"{queue}:",
        CELERY_VISIBILITY_TIMEOUT_SECONDS="5",
        JOB_LEASE_SECONDS="6",
        JOB_HEARTBEAT_INTERVAL_SECONDS="1",
        OBJECT_STORAGE_ENDPOINT=storage_endpoint,
        OBJECT_STORAGE_EXTERNAL_ENDPOINT=storage_endpoint,
        OBJECT_STORAGE_ACCESS_KEY=access_key,
        OBJECT_STORAGE_SECRET_KEY=secret_key,
        OBJECT_STORAGE_BUCKET=bucket,
        JOB_SOURCE_ENCRYPTION_KEY="VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ=",
        BACKEND_URL=f"http://127.0.0.1:{port}",
        PYTHONDONTWRITEBYTECODE="1",
        ENVIRONMENT="test",
    )
    run_temp = Path(tempfile.mkdtemp(prefix=f"issue35-{run_id}-"))
    publisher_ready = run_temp / "publisher.ready"
    process_registry_path = run_temp / "process-registry.json"
    process_registry = ProcessRegistry(process_registry_path)
    process_registry.start_monitor()
    cleanup_receipt_value = os.environ.get("ASYNC_CLEANUP_RECEIPT")
    cleanup_receipt = Path(cleanup_receipt_value) if cleanup_receipt_value else None
    if cleanup_receipt:
        cleanup_receipt.unlink(missing_ok=True)
    _prepare_ready_file(publisher_ready)
    env["OUTBOX_READY_FILE"] = str(publisher_ready)
    children: list[subprocess.Popen[bytes]] = []
    service_children: list[subprocess.Popen[bytes]] = []
    logs: list[Path] = []
    s3 = boto3.client(
        "s3",
        endpoint_url=storage_endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.environ.get("OBJECT_STORAGE_REGION", "us-east-1"),
    )
    interrupted = False
    test_exit_code = 1

    def cleanup_redis() -> None:
        client = redis.Redis(
            host=redis_parts.hostname or "localhost",
            port=redis_parts.port or 6379,
            db=redis_db,
        )
        for key in client.scan_iter(match=f"*{queue}*"):
            if queue.encode() not in key:
                raise RuntimeError("Redis namespace mismatch")
            client.unlink(key)

    def request_stop(*_unused: object) -> None:
        nonlocal interrupted
        interrupted = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(f'CREATE DATABASE "{database_name}"')
        migration = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=backend,
            env=env,
            capture_output=True,
            timeout=120,
        )
        if migration.returncode != 0:
            raise RuntimeError(
                "Migration failed: "
                + _redact(migration.stderr.decode(), sensitive_values)
            )
        with psycopg.connect(database_url) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        if revision != (TARGET_REVISION,):
            raise RuntimeError("Migration did not reach the required head")
        s3.create_bucket(Bucket=bucket)

        if sys.platform == "win32":
            loop_factory = (
                "lambda:asyncio.SelectorEventLoop(selectors.SelectSelector())"
            )
            backend_bootstrap = (
                "import asyncio,selectors,uvicorn;"
                "from app.main import app;"
                f"server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port={port}));"
                f"asyncio.run(server.serve(),loop_factory={loop_factory})"
            )
            publisher_bootstrap = (
                "import asyncio,selectors;"
                "from app.outbox.publisher import run;"
                f"asyncio.run(run(),loop_factory={loop_factory})"
            )
        else:
            backend_bootstrap = (
                "import uvicorn;"
                f"uvicorn.run('app.main:app',host='127.0.0.1',port={port})"
            )
            publisher_bootstrap = "from app.outbox.publisher import main;main()"
        for name, command, cwd in (
            (
                "backend",
                [str(backend_python), "-c", backend_bootstrap],
                backend,
            ),
            (
                "publisher",
                [str(backend_python), "-c", publisher_bootstrap],
                backend,
            ),
        ):
            log_path = Path(tempfile.gettempdir()) / f"issue35-{run_id}-{name}.log"
            logs.append(log_path)
            stream = log_path.open("wb")
            flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            )
            child = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            stream.close()
            children.append(child)
            service_children.append(child)
            process_registry.register_root(child.pid)
            if name == "backend":
                _wait_ready(child, port, 30)
            else:
                _wait_file_ready(child, publisher_ready, 30)

        pytest_args = args.pytest_args or ["tests"]
        if pytest_args and pytest_args[0] == "--":
            pytest_args = pytest_args[1:]
        pytest_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        )
        pytest_process = subprocess.Popen(
            ["uv", "run", "pytest", *pytest_args],
            cwd=worker,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=pytest_flags,
            start_new_session=sys.platform != "win32",
        )
        children.append(pytest_process)
        process_registry.register_root(pytest_process.pid)
        try:
            test_exit_code, bounded_output = _wait_for_pytest(
                pytest_process, timeout=1200
            )
        except subprocess.TimeoutExpired:
            test_exit_code = 1
            raise RuntimeError("Pytest timed out") from None
        if test_exit_code != 0 or interrupted:
            raise RuntimeError("Pytest failed")
        if any(child.poll() is not None for child in service_children):
            raise RuntimeError("Managed service exited before test completion")
        if re.search(r"\bskipped\b", bounded_output.decode(errors="replace")):
            raise RuntimeError("Integration run contained skipped tests")
        test_exit_code = 0
    except KeyboardInterrupt:
        test_exit_code = 1
    except Exception:
        print("run_failed=RUN_FAILED", file=sys.stderr)
        if test_exit_code == 0:
            test_exit_code = 1
    finally:

        def verify_redis() -> None:
            client = redis.Redis(
                host=redis_parts.hostname or "localhost",
                port=redis_parts.port or 6379,
                db=redis_db,
            )
            if any(client.scan_iter(match=f"*{queue}*")):
                raise RuntimeError("Redis cleanup verification failed")

        def cleanup_storage() -> None:
            token = None
            while True:
                arguments = {"Bucket": bucket}
                if token:
                    arguments["ContinuationToken"] = token
                response = s3.list_objects_v2(**arguments)
                for item in response.get("Contents", []):
                    s3.delete_object(Bucket=bucket, Key=item["Key"])
                if not response.get("IsTruncated"):
                    break
                token = response["NextContinuationToken"]
            s3.delete_bucket(Bucket=bucket)

        def verify_storage() -> None:
            try:
                s3.head_bucket(Bucket=bucket)
            except ClientError as error:
                if (
                    error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    == 404
                ):
                    return
                raise RuntimeError("Storage cleanup verification failed") from None
            raise RuntimeError("Storage cleanup verification failed")

        def cleanup_database() -> None:
            with psycopg.connect(admin_url, autocommit=True) as connection:
                connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=%s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                connection.execute(f'DROP DATABASE IF EXISTS "{database_name}"')

        def verify_database() -> None:
            with psycopg.connect(admin_url) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (database_name,)
                ).fetchone()
            if exists:
                raise RuntimeError("Database cleanup verification failed")

        def cleanup_logs() -> None:
            for path in logs:
                path.unlink(missing_ok=True)

        def cleanup_ready_file() -> None:
            publisher_ready.unlink(missing_ok=True)

        def cleanup_temp() -> None:
            run_temp.rmdir()

        def verify_temp() -> None:
            if run_temp.exists():
                raise RuntimeError("Temporary directory cleanup verification failed")

        cleanup_operations = {
            "PROCESS_TREE_STOP": process_registry.stop_all,
            "PROCESS_TREE_VERIFY": process_registry.verify_empty,
            "REDIS_CLEANUP": cleanup_redis,
            "REDIS_VERIFY": verify_redis,
            "STORAGE_CLEANUP": cleanup_storage,
            "STORAGE_VERIFY": verify_storage,
            "DATABASE_CLEANUP": cleanup_database,
            "DATABASE_VERIFY": verify_database,
            "LOG_CLEANUP": cleanup_logs,
            "READY_FILE_CLEANUP": cleanup_ready_file,
            "PROCESS_REGISTRY_CLEANUP": process_registry.remove,
            "TEMP_CLEANUP": cleanup_temp,
            "TEMP_VERIFY": verify_temp,
        }

    def completed_execution() -> int:
        if interrupted:
            raise KeyboardInterrupt
        return test_exit_code

    return _execute_lifecycle(
        completed_execution,
        cleanup_operations,
        cleanup_receipt,
    )


if __name__ == "__main__":
    raise SystemExit(main())
