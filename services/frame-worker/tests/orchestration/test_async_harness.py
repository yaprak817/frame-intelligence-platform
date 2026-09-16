import importlib.util
import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "run_async_integration.py"
SPEC = importlib.util.spec_from_file_location("run_async_integration", SCRIPT)
assert SPEC and SPEC.loader
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def _operations(failures: set[str], calls: list[str]):
    def operation(code):
        def action():
            calls.append(code)
            if code in failures:
                raise RuntimeError("credential=https://internal.invalid/db")

        return action

    return {code: operation(code) for code in harness.REQUIRED_CLEANUP_STEPS}


@pytest.mark.parametrize(
    "code",
    [
        "DATABASE_CLEANUP",
        "REDIS_CLEANUP",
        "STORAGE_CLEANUP",
        "PROCESS_TREE_STOP",
        "READY_FILE_CLEANUP",
        "TEMP_CLEANUP",
    ],
)
def test_real_main_reports_each_cleanup_failure_and_continues(
    tmp_path, code, capsys
) -> None:
    calls: list[str] = []
    receipt = tmp_path / "receipt.json"
    result = harness.main(
        cleanup_operations=_operations({code}, calls),
        test_exit_code=0,
        cleanup_receipt=receipt,
    )
    assert result == harness.CLEANUP_FAILURE_EXIT
    assert calls == list(harness.REQUIRED_CLEANUP_STEPS)
    assert not receipt.exists()
    captured = capsys.readouterr()
    assert captured.err.strip() == f"cleanup_failed={code}"


def test_real_main_collects_safe_codes_and_preserves_pytest_exit(tmp_path, capsys):
    calls: list[str] = []
    failures = {"DATABASE_CLEANUP", "REDIS_CLEANUP", "STORAGE_CLEANUP"}
    result = harness.main(
        cleanup_operations=_operations(failures, calls),
        test_exit_code=7,
        cleanup_receipt=tmp_path / "receipt.json",
    )
    assert result == 7
    assert calls == list(harness.REQUIRED_CLEANUP_STEPS)
    stderr = capsys.readouterr().err
    assert stderr.strip() == (
        "cleanup_failed=REDIS_CLEANUP,STORAGE_CLEANUP,DATABASE_CLEANUP"
    )
    assert "credential" not in stderr
    assert "internal" not in stderr


def test_real_main_publishes_receipt_only_after_exact_success(tmp_path):
    calls: list[str] = []
    receipt = tmp_path / "receipt.json"
    assert (
        harness.main(
            cleanup_operations=_operations(set(), calls),
            test_exit_code=0,
            cleanup_receipt=receipt,
        )
        == 0
    )
    assert json.loads(receipt.read_text(encoding="ascii")) == {
        "status": "clean",
        "verified": list(harness.REQUIRED_CLEANUP_STEPS),
    }


def test_real_main_handles_keyboard_interrupt_before_common_cleanup(
    tmp_path, capsys
) -> None:
    calls: list[str] = []
    receipt = tmp_path / "receipt.json"

    def interrupt() -> int:
        raise KeyboardInterrupt

    result = harness.main(
        cleanup_operations=_operations(set(), calls),
        cleanup_receipt=receipt,
        execute=interrupt,
    )
    assert result == 1
    assert calls == list(harness.REQUIRED_CLEANUP_STEPS)
    assert not receipt.exists()
    assert capsys.readouterr().err.strip() == "run_failed=RUN_INTERRUPTED"


def test_operational_failure_is_fail_closed_for_all_canaries(tmp_path, capsys) -> None:
    calls: list[str] = []
    canaries = (
        "frame_async_canary",
        "frame-async-canary",
        "jobs/canary/results/object.jpg",
        "192.0.2.4",
        "2001:db8::4",
        "https://example.invalid/object?X-Amz-Signature=canary",
        "postgresql://user:password@example.invalid/db",
        "redis://:secret@example.invalid/4",
        "s3://bucket/private/key",
        "Authorization: Bearer bearer-canary",
        "AKIAABCDEFGHIJKLMNOP",
    )

    def fail() -> int:
        raise RuntimeError(" ".join(canaries))

    result = harness.main(
        cleanup_operations=_operations(set(), calls),
        cleanup_receipt=tmp_path / "receipt.json",
        execute=fail,
    )
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.strip() == "run_failed=RUN_FAILED"
    assert all(canary not in captured.err for canary in canaries)


@pytest.mark.parametrize("exit_code,status", [(0, "PASSED"), (7, "FAILED")])
def test_real_pytest_status_publication_never_replays_child_output(
    capsys, exit_code, status
) -> None:
    canaries = (
        "frame_async_canary",
        "frame-async-canary",
        "datasets/job-id/run-id/attempt-token/image.jpg",
        r"C:\private\datasets\image.jpg",
        "/private/datasets/image.jpg",
        "postgresql://user:password@example.invalid/db",
        "redis://:secret@example.invalid/4",
        "https://example.invalid/object?X-Amz-Signature=canary",
        "192.0.2.4",
        "2001:db8::4",
        "Authorization: Bearer bearer-canary",
        "password=password-canary secret=secret-canary token=token-canary",
        "credential=credential-canary",
        "AKIAABCDEFGHIJKLMNOP",
        "Traceback: RuntimeError: raw exception canary",
        "ordinary child output must not be replayed",
    )
    fake_process = SimpleNamespace(
        returncode=exit_code,
        stdout=io.BytesIO("\n".join(canaries).encode()),
        stderr=io.BytesIO("\n".join(reversed(canaries)).encode()),
        wait=lambda *, timeout: exit_code,
    )

    actual_exit_code, bounded_output = harness._wait_for_pytest(fake_process, timeout=1)
    assert actual_exit_code == exit_code
    assert all(value.encode() in bounded_output for value in canaries)

    captured = capsys.readouterr()
    assert captured.out == f"pytest_status={status} exit_code={exit_code}\n"
    assert captured.err == ""
    assert not any(value in captured.out + captured.err for value in canaries)


def test_structured_redaction_covers_labeled_sensitive_fields() -> None:
    labels = (
        "database",
        "bucket",
        "object_key",
        "object_path",
        "namespace",
        "dsn",
        "password",
        "secret",
        "token",
        "key",
        "credential",
    )
    source = " ".join(f"{label}=canary-{index}" for index, label in enumerate(labels))
    redacted = harness._redact(source)
    assert redacted == " ".join(f"{label}=[REDACTED]" for label in labels)


def _write_junit(path: Path, testcases: str) -> None:
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>{testcases}'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )


def test_junit_diagnostic_reports_first_failure_deterministically(
    tmp_path, capsys
) -> None:
    report = tmp_path / "report.xml"
    _write_junit(
        report,
        '<testcase classname="tests.first" name="test_failed">'
        '<failure type="AssertionError" message="assert 2 == 3" /></testcase>'
        '<testcase classname="tests.second" name="test_error">'
        '<error type="RuntimeError" message="later error" /></testcase>',
    )

    harness._publish_pytest_failure(report)

    assert capsys.readouterr().out.splitlines() == [
        "pytest_failure_node=tests.first::test_failed",
        "pytest_failure_type=AssertionError",
        "pytest_failure_summary=assert 2 == 3",
    ]


def test_junit_diagnostic_preserves_error_before_later_failure(
    tmp_path, capsys
) -> None:
    report = tmp_path / "report.xml"
    _write_junit(
        report,
        '<testcase classname="tests.first" name="test_error">'
        '<error type="RuntimeError" message="first error" /></testcase>'
        '<testcase classname="tests.second" name="test_failed">'
        '<failure type="AssertionError" message="later failure" /></testcase>',
    )

    harness._publish_pytest_failure(report)

    output = capsys.readouterr().out
    assert "pytest_failure_node=tests.first::test_error" in output
    assert "pytest_failure_type=RuntimeError" in output
    assert "later failure" not in output


DRIVER_DATABASE_URL = (
    "postgresql+psycopg://diagnostic_user:diagnostic_password@"
    "127.0.0.1:5432/diagnostic_db"
)
DIAGNOSTIC_CANARIES = (
    ("driver_database_url", DRIVER_DATABASE_URL, True),
    (
        "normalized_database_url",
        "postgresql://normalized_user:normalized_password@127.0.0.1:5432/normalized_db",
        True,
    ),
    ("redis_url", "redis://:diagnostic_password@127.0.0.1:6379/7", True),
    ("minio_endpoint", "http://127.0.0.1:9000", True),
    ("bucket", "bucket7", True),
    ("queue", "queue7", True),
    ("runtime_id", "task7", True),
    ("object_key", "jobs/task7/frames/object.jpg", False),
    ("object_prefix", "jobs/task7", False),
)


@pytest.mark.parametrize("field", ["node", "type", "summary"])
@pytest.mark.parametrize("_kind,canary,use_exact", DIAGNOSTIC_CANARIES)
def test_junit_diagnostic_redacts_each_field_without_relying_on_truncation(
    tmp_path, capsys, field, _kind, canary, use_exact
) -> None:
    report = tmp_path / "report.xml"
    node = f"case-{canary}" if field == "node" else "case-safe"
    failure_type = f"Error-{canary}" if field == "type" else "SafeError"
    message = f"failed {canary}" if field == "summary" else "safe failure"
    raw_value = {
        "node": f"tests.safe::{node}",
        "type": failure_type,
        "summary": message,
    }[field]
    field_limit = {
        "node": harness.PYTEST_DIAGNOSTIC_NODE_MAX_CHARS,
        "type": harness.PYTEST_DIAGNOSTIC_TYPE_MAX_CHARS,
        "summary": harness.PYTEST_DIAGNOSTIC_MAX_CHARS,
    }[field]
    assert canary in raw_value
    assert len(raw_value) < field_limit
    _write_junit(
        report,
        f'<testcase classname="tests.safe" name="{node}">'
        f'<failure type="{failure_type}" message="{message}" /></testcase>',
    )

    harness._publish_pytest_failure(report, (canary,) if use_exact else ())

    output = capsys.readouterr().out
    assert canary not in output
    assert "[REDACTED]" in output
    if canary == DRIVER_DATABASE_URL:
        assert "diagnostic_user" not in output
        assert "diagnostic_password" not in output
        assert "diagnostic_db" not in output


def test_junit_diagnostic_is_bounded_to_three_lines_and_500_chars(
    tmp_path, capsys
) -> None:
    report = tmp_path / "report.xml"
    node = " ".join(["node"] * 100)
    failure_type = " ".join(["kind"] * 50)
    message = " ".join(["summary"] * 100)
    _write_junit(
        report,
        f'<testcase classname="tests.bounds" name="{node}">'
        f'<failure type="{failure_type}" message="{message}" /></testcase>',
    )

    harness._publish_pytest_failure(report)
    first_output = capsys.readouterr().out
    harness._publish_pytest_failure(report)
    second_output = capsys.readouterr().out
    output = first_output
    lines = output.splitlines()
    assert len(output) == harness.PYTEST_DIAGNOSTIC_MAX_CHARS == 500
    assert output.endswith("\n")
    assert output == second_output
    assert len(lines) == 3
    assert [line.split("=", 1)[0] for line in lines] == [
        "pytest_failure_node",
        "pytest_failure_type",
        "pytest_failure_summary",
    ]
    assert all(line.count("=") == 1 for line in lines)
    assert all(
        output.count(f"{field}=") == 1
        for field in (
            "pytest_failure_node",
            "pytest_failure_type",
            "pytest_failure_summary",
        )
    )


def _fake_pytest_process(
    exit_code: int,
    command: list[str] | None = None,
    junit_contents: str | None = None,
) -> SimpleNamespace:
    def wait(*, timeout):
        assert timeout > 0
        if command is not None:
            junit_option = command.index("--junitxml")
            junit_path = Path(command[junit_option + 1])
            assert junit_path.is_absolute()
            assert not junit_path.exists()
            if junit_contents is not None:
                junit_path.write_text(junit_contents, encoding="utf-8")
        return exit_code

    return SimpleNamespace(
        returncode=exit_code,
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
        wait=wait,
    )


@pytest.mark.parametrize("contents,exit_code", [(None, 41), ("", 42), ("not xml", 43)])
def test_junit_unavailable_lifecycle_preserves_exit_and_cleanup_priority(
    tmp_path, capsys, contents, exit_code
) -> None:
    run_temp = (tmp_path / "run").resolve()
    run_temp.mkdir()
    report, cleanup_paths = harness._pytest_artifacts(run_temp)
    command = harness._pytest_command(["tests"], report)
    actual_exit, _output = harness._complete_pytest_run(
        _fake_pytest_process(exit_code, command, contents), report, (), timeout=1
    )
    cleanup_calls: list[str] = []
    cleanup_operations = _operations(set(), cleanup_calls)

    def cleanup_junit() -> None:
        cleanup_calls.append("LOG_CLEANUP")
        harness._cleanup_log_paths(cleanup_paths)

    cleanup_operations["LOG_CLEANUP"] = cleanup_junit
    lifecycle_exit = harness.main(
        cleanup_operations=cleanup_operations,
        test_exit_code=actual_exit,
        cleanup_receipt=tmp_path / "receipt.json",
    )

    captured = capsys.readouterr()
    assert actual_exit == lifecycle_exit == exit_code
    assert f"pytest_status=FAILED exit_code={exit_code}" in captured.out
    assert "pytest_failure_summary=UNAVAILABLE\n" in captured.out
    assert "ParseError" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
    assert "not xml" not in captured.out + captured.err
    assert cleanup_calls == list(harness.REQUIRED_CLEANUP_STEPS)
    assert not report.exists()
    assert (tmp_path / "receipt.json").is_file()


def test_passing_pytest_lifecycle_has_no_failure_diagnostic(
    tmp_path, capsys, monkeypatch
) -> None:
    run_temp = (tmp_path / "run").resolve()
    run_temp.mkdir()
    report, cleanup_paths = harness._pytest_artifacts(run_temp)
    junit_contents = (
        '<testsuites><testsuite><testcase classname="tests.pass" '
        'name="test_pass" /></testsuite></testsuites>'
    )
    calls = []
    monkeypatch.setattr(
        harness,
        "_publish_pytest_failure",
        lambda *_args, **_kwargs: calls.append("failure"),
    )

    command = harness._pytest_command(["tests"], report)
    assert not report.exists()
    exit_code, _output = harness._complete_pytest_run(
        _fake_pytest_process(0, command, junit_contents),
        report,
        ("db7",),
        timeout=1,
    )
    receipt = tmp_path / "receipt.json"
    cleanup_calls: list[str] = []
    cleanup_operations = _operations(set(), cleanup_calls)

    def cleanup_junit() -> None:
        cleanup_calls.append("LOG_CLEANUP")
        harness._cleanup_log_paths(cleanup_paths)

    cleanup_operations["LOG_CLEANUP"] = cleanup_junit
    lifecycle_exit = harness.main(
        cleanup_operations=cleanup_operations,
        test_exit_code=exit_code,
        cleanup_receipt=receipt,
    )

    captured = capsys.readouterr()
    assert command == ["uv", "run", "pytest", "tests", "--junitxml", str(report)]
    assert report.is_absolute()
    assert report.parent == run_temp
    assert calls == []
    assert "pytest_failure_" not in captured.out + captured.err
    assert not report.exists()
    assert lifecycle_exit == 0
    assert receipt.is_file()
    assert cleanup_calls == list(harness.REQUIRED_CLEANUP_STEPS)


def test_failing_pytest_lifecycle_reports_then_cleans_and_preserves_exit(
    tmp_path, capsys, monkeypatch
) -> None:
    run_temp = (tmp_path / "run").resolve()
    run_temp.mkdir()
    report, cleanup_paths = harness._pytest_artifacts(run_temp)
    junit_contents = (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>'
        '<testcase classname="tests.fail" name="test_failed-db7">'
        '<failure type="AssertionError" message="db7 failed" /></testcase>'
        "</testsuite></testsuites>"
    )
    sensitive_values = ("db7",)
    publish_calls = []
    original_publish = harness._publish_pytest_failure

    def publish(path, values):
        publish_calls.append((path, values))
        original_publish(path, values)

    monkeypatch.setattr(harness, "_publish_pytest_failure", publish)

    command = harness._pytest_command(["tests"], report)
    assert not report.exists()
    exit_code, _output = harness._complete_pytest_run(
        _fake_pytest_process(7, command, junit_contents),
        report,
        sensitive_values,
        timeout=1,
    )
    cleanup_calls: list[str] = []
    cleanup_operations = _operations({"DATABASE_CLEANUP"}, cleanup_calls)

    def cleanup_junit() -> None:
        cleanup_calls.append("LOG_CLEANUP")
        harness._cleanup_log_paths(cleanup_paths)

    cleanup_operations["LOG_CLEANUP"] = cleanup_junit
    lifecycle_exit = harness.main(
        cleanup_operations=cleanup_operations,
        test_exit_code=exit_code,
        cleanup_receipt=tmp_path / "receipt.json",
    )

    captured = capsys.readouterr()
    assert publish_calls == [(report, sensitive_values)]
    assert captured.out.splitlines()[:4] == [
        "pytest_status=FAILED exit_code=7",
        "pytest_failure_node=tests.fail::test_failed-[REDACTED]",
        "pytest_failure_type=AssertionError",
        "pytest_failure_summary=[REDACTED] failed",
    ]
    assert not report.exists()
    assert lifecycle_exit == 7
    assert not (tmp_path / "receipt.json").exists()
    assert cleanup_calls == list(harness.REQUIRED_CLEANUP_STEPS)
    assert "cleanup_failed=DATABASE_CLEANUP" in captured.err


@pytest.mark.parametrize("exit_code", [1, 7])
def test_failure_diagnostic_does_not_change_pytest_exit_code(
    tmp_path, capsys, exit_code
) -> None:
    report = tmp_path / "report.xml"
    _write_junit(
        report,
        '<testcase classname="tests.exit" name="test_failed">'
        '<failure type="AssertionError" message="safe" /></testcase>',
    )
    actual_exit_code, _output = harness._complete_pytest_run(
        _fake_pytest_process(exit_code), report, (), timeout=1
    )

    assert actual_exit_code == exit_code
    assert "pytest_failure_summary=safe" in capsys.readouterr().out


def test_harness_requires_current_single_migration_head() -> None:
    assert harness.TARGET_REVISION == "20260914_0008"
    assert harness.DEPENDENCY_PREP_TIMEOUT_SECONDS == 300
    assert harness.MIGRATION_TIMEOUT_SECONDS == 120


def test_database_url_normalizes_only_localhost_for_windows_selector_loop() -> None:
    local = harness._database_url(
        "postgresql://user:password@localhost:5432/postgres", "task"
    )
    remote = harness._database_url(
        "postgresql://user:password@database.internal:5432/postgres", "task"
    )
    assert "@127.0.0.1:5432/task" in local
    assert "@database.internal:5432/task" in remote


def test_storage_cleanup_before_bucket_creation_is_a_safe_noop() -> None:
    class UnexpectedClient:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected storage call: {name}")

    harness._cleanup_task_bucket(UnexpectedClient(), "task-owned", created=False)


def test_storage_cleanup_aborts_multipart_and_removes_versions_and_objects() -> None:
    calls = []

    class Client:
        inventory = [{"Key": "object"}]

        def list_multipart_uploads(self, **_kwargs):
            return {"Uploads": [{"Key": "partial", "UploadId": "upload"}]}

        def abort_multipart_upload(self, **kwargs):
            calls.append(("abort", kwargs["Key"]))

        def list_object_versions(self, **_kwargs):
            return {
                "Versions": [{"Key": "versioned", "VersionId": "v1"}],
                "DeleteMarkers": [{"Key": "deleted", "VersionId": "v2"}],
            }

        def delete_objects(self, **kwargs):
            calls.append(("delete", len(kwargs["Delete"]["Objects"])))
            self.inventory = []

        def list_objects_v2(self, **_kwargs):
            return {"Contents": self.inventory}

        def delete_bucket(self, **_kwargs):
            calls.append(("bucket", None))

    harness._cleanup_task_bucket(Client(), "task-owned", created=True)
    assert calls == [("abort", "partial"), ("delete", 2), ("bucket", None)]


def test_storage_cleanup_retry_exhaustion_is_bounded() -> None:
    attempts = []

    class Client:
        def list_multipart_uploads(self, **_kwargs):
            attempts.append(1)
            raise RuntimeError("secret storage failure")

    with pytest.raises(RuntimeError, match="secret storage failure"):
        harness._cleanup_task_bucket(
            Client(), "task-owned", created=True, sleep=lambda _seconds: None
        )
    assert len(attempts) == harness.STORAGE_CLEANUP_MAX_ATTEMPTS


@pytest.mark.parametrize("value", [-1, 256, True, None, "7"])
def test_pytest_status_bounds_untrusted_exit_code(value, capsys) -> None:
    assert harness._publish_pytest_status(value) == 1
    assert capsys.readouterr().out == "pytest_status=FAILED exit_code=1\n"


WINDOWS_CONTROL_EVENT = 0x1FF


def _windows_registry(
    tmp_path, ignore_graceful, *, control_event=WINDOWS_CONTROL_EVENT
):
    identity = harness.ProcessIdentity(12345, 1, "created")
    live = [identity]
    signals = []
    ticks = iter(range(0, 1_000, 20))

    def kill(pid, sent_signal):
        assert pid == identity.pid
        signals.append(sent_signal)
        if sent_signal != control_event or not ignore_graceful:
            live.clear()

    registry = harness.ProcessRegistry(
        tmp_path / "registry.json",
        windows=True,
        windows_control_event=control_event,
        signal_sender=kill,
        monotonic=lambda: next(ticks),
        sleep=lambda _seconds: None,
    )
    registry._roots = {identity.pid}
    registry._owned = {identity.pid: identity}
    registry.stop_monitor = lambda: None
    registry._matching_live = lambda: list(live)
    return registry, signals


def test_windows_graceful_exit_does_not_force(tmp_path) -> None:
    registry, signals = _windows_registry(tmp_path, False)
    registry.stop_all()
    assert signals == [WINDOWS_CONTROL_EVENT]


def test_windows_ignored_graceful_signal_uses_bounded_force(
    tmp_path,
) -> None:
    registry, signals = _windows_registry(tmp_path, True)
    registry.stop_all()
    assert signals == [WINDOWS_CONTROL_EVENT, harness.signal.SIGTERM]


def test_windows_graceful_sender_oserror_still_uses_force(
    tmp_path,
) -> None:
    registry, signals = _windows_registry(tmp_path, True)
    original_kill = registry._signal_sender

    def kill(pid, sent_signal):
        if sent_signal == WINDOWS_CONTROL_EVENT:
            signals.append(sent_signal)
            raise OSError("console group is unavailable")
        original_kill(pid, sent_signal)

    registry._signal_sender = kill
    registry.stop_all()
    assert signals == [WINDOWS_CONTROL_EVENT, harness.signal.SIGTERM]


def test_windows_unavailable_control_event_uses_force(tmp_path) -> None:
    registry, signals = _windows_registry(tmp_path, True, control_event=None)
    registry.stop_all()
    assert signals == [harness.signal.SIGTERM]


def _wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.02)
    raise AssertionError("bounded wait timed out")


def _tree_process(tmp_path):
    grandchild = tmp_path / "grandchild.py"
    child = tmp_path / "child.py"
    parent = tmp_path / "parent.py"
    grandchild.write_text(
        "import pathlib,os,threading\n"
        f"p=pathlib.Path({str(tmp_path / 'grandchild.pid')!r})\n"
        "p.write_text(str(os.getpid()))\n"
        "threading.Event().wait()\n",
        encoding="utf-8",
    )
    child.write_text(
        "import pathlib,subprocess,sys,threading,os\n"
        f"subprocess.Popen([sys.executable,{str(grandchild)!r}])\n"
        f"pathlib.Path({str(tmp_path / 'child.pid')!r}).write_text(str(os.getpid()))\n"
        "threading.Event().wait()\n",
        encoding="utf-8",
    )
    parent.write_text(
        "import pathlib,subprocess,sys,threading,os\n"
        f"subprocess.Popen([sys.executable,{str(child)!r}])\n"
        f"pathlib.Path({str(tmp_path / 'parent.pid')!r}).write_text(str(os.getpid()))\n"
        "threading.Event().wait()\n",
        encoding="utf-8",
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(
        [sys.executable, str(parent)],
        creationflags=flags,
        start_new_session=sys.platform != "win32",
    )


@pytest.mark.parametrize("failure_mode", ["timeout", "crash"])
def test_main_cleans_owned_tree_and_preserves_sentinel(tmp_path, failure_mode):
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import threading;threading.Event().wait()"]
    )
    parent = _tree_process(tmp_path)
    registry = harness.ProcessRegistry(tmp_path / "registry.json")
    receipt = tmp_path / "receipt.json"
    try:
        registry.register_root(parent.pid)
        registry.start_monitor()
        _wait_for(
            lambda: all(
                (tmp_path / f"{name}.pid").exists()
                for name in ("parent", "child", "grandchild")
            )
        )
        _wait_for(
            lambda: len(json.loads(registry.path.read_text(encoding="ascii"))) >= 3
        )
        registered = {
            item["pid"]
            for item in json.loads(registry.path.read_text(encoding="ascii"))
        }
        expected = {
            int((tmp_path / f"{name}.pid").read_text())
            for name in ("parent", "child", "grandchild")
        }
        assert registered >= expected
        if failure_mode == "crash":
            parent.kill()
            parent.wait(timeout=5)
        calls: list[str] = []
        operations = _operations(set(), calls)
        operations.update(
            PROCESS_TREE_STOP=registry.stop_all,
            PROCESS_TREE_VERIFY=registry.verify_empty,
            PROCESS_REGISTRY_CLEANUP=registry.remove,
        )
        assert harness.main(
            cleanup_operations=operations,
            test_exit_code=1 if failure_mode == "crash" else 0,
            cleanup_receipt=receipt,
        ) == (1 if failure_mode == "crash" else 0)
        assert sentinel.poll() is None
        assert not registry.path.exists()
        assert receipt.is_file()
    finally:
        if parent.poll() is None:
            parent.kill()
        sentinel.kill()
        sentinel.wait(timeout=5)


def test_registry_ownership_mismatch_does_not_kill_process(tmp_path, monkeypatch):
    process = subprocess.Popen(
        [sys.executable, "-c", "import threading;threading.Event().wait()"]
    )
    registry = harness.ProcessRegistry(tmp_path / "registry.json")
    try:
        registry.register_root(process.pid)
        original = harness._process_snapshot
        identity = original()[process.pid]
        mismatch = identity._replace(created=identity.created + "-reused")
        monkeypatch.setattr(
            harness, "_process_snapshot", lambda: {process.pid: mismatch}
        )
        with pytest.raises(RuntimeError, match="ownership mismatch"):
            registry.stop_all()
        assert process.poll() is None
        calls: list[str] = []
        operations = _operations(set(), calls)
        operations["PROCESS_TREE_STOP"] = registry.stop_all
        assert harness.main(cleanup_operations=operations, test_exit_code=0) == 2
    finally:
        process.kill()
        process.wait(timeout=5)


def test_publisher_ready_file_is_positive_bounded_and_not_stale(tmp_path) -> None:
    ready = tmp_path / "publisher.ready"
    ready.write_text("ready", encoding="ascii")
    harness._prepare_ready_file(ready)
    assert not ready.exists()
    ready.write_text("ready", encoding="ascii")
    harness._wait_file_ready(SimpleNamespace(poll=lambda: None), ready, 0.2)


def test_publisher_readiness_timeout_and_early_exit(tmp_path) -> None:
    ready = tmp_path / "publisher.ready"
    try:
        harness._wait_file_ready(SimpleNamespace(poll=lambda: None), ready, 0.01)
    except TimeoutError:
        pass
    else:
        raise AssertionError("missing readiness must time out")
    try:
        harness._wait_file_ready(SimpleNamespace(poll=lambda: 1), ready, 0.2)
    except RuntimeError as error:
        assert str(error) == "Managed publisher exited before readiness"
    else:
        raise AssertionError("early publisher exit must fail")
