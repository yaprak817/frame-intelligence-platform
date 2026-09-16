import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select

from frame_worker.orchestration import training
from frame_worker.orchestration.training import Claim, TrainingError, _terminate


def _alive(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists() and stat.read_text().split()[2] == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_failed_termination_skips_artifact_and_temp_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    class Client:
        def delete_object(self, **_kwargs):
            pytest.fail("artifact cleanup ran while trainer may be alive")

    token, training_id, project_id = uuid4(), uuid4(), uuid4()
    claim = Claim(
        {"id": training_id, "project_id": project_id},
        token,
        1,
        f"annotations/{project_id}/trainings/{training_id}/working/1-{token}/",
    )
    root = training._training_root(tmp_path, claim)
    root.mkdir(parents=True)
    marker = root / "trainer-file"
    marker.write_text("keep", encoding="ascii")

    def fail(_process):
        raise RuntimeError("raw trainer path and command")

    monkeypatch.setattr(training, "_terminate", fail)

    def forbid_temp_cleanup(*_args, **_kwargs):
        pytest.fail("temp cleanup ran while trainer may be alive")

    monkeypatch.setattr(training.shutil, "rmtree", forbid_temp_cleanup)
    with pytest.raises(
        TrainingError, match="Training process cleanup failure"
    ) as error:
        training._cleanup_training(
            object(),
            Client(),
            "private",
            claim,
            [f"annotations/{project_id}/trainings/{training_id}/model.pt"],
            [claim.prefix + "snapshot.zip"],
            root,
        )
    assert "raw trainer" not in str(error.value)
    assert error.value.__cause__ is None
    assert marker.read_text(encoding="ascii") == "keep"


@pytest.mark.parametrize("failure", ["termination", "group-verification"])
def test_failed_termination_fences_database_and_prevents_new_claim(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    if failure == "group-verification" and not sys.platform.startswith("linux"):
        pytest.skip("Linux process-group verification required")
    training_id, project_id, token = uuid4(), uuid4(), uuid4()
    claim = Claim(
        {"id": training_id, "project_id": project_id},
        token,
        1,
        f"annotations/{project_id}/trainings/{training_id}/working/1-{token}/",
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'training.db'}")
    training.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(training.projects).values(id=project_id))
        connection.execute(
            insert(training.runs).values(
                id=training_id,
                project_id=project_id,
                status="RUNNING",
                lease_token=token,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                attempt_generation=1,
                working_prefix=claim.prefix,
            )
        )
    root = training._training_root(tmp_path, claim)
    root.mkdir(parents=True)
    marker = root / "trainer-file"
    marker.write_text("keep", encoding="ascii")

    def fail(_process):
        raise RuntimeError("raw trainer command")

    def forbid_temp_cleanup(*_args, **_kwargs):
        pytest.fail("temp cleanup ran while trainer may be alive")

    class Client:
        def delete_object(self, **_kwargs):
            pytest.fail("artifact cleanup ran while trainer may be alive")

    if failure == "termination":
        process = object()
        monkeypatch.setattr(training, "_terminate", fail)
    else:
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"], start_new_session=True
        )
        process.wait(timeout=5)
        monkeypatch.setattr(training, "_group_alive", lambda _group_id: True)
    monkeypatch.setattr(training.shutil, "rmtree", forbid_temp_cleanup)
    try:
        with pytest.raises(
            TrainingError, match="Training process cleanup failure"
        ) as error:
            training._finalize_failed_attempt(
                engine,
                Client(),
                "private",
                claim,
                process,
                [f"annotations/{project_id}/trainings/{training_id}/model.pt"],
                [claim.prefix + "snapshot.zip"],
                root,
                None,
            )
    finally:
        if failure == "group-verification" and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert error.value.__cause__ is None
    assert marker.read_text(encoding="ascii") == "keep"
    with engine.connect() as connection:
        row = connection.execute(
            select(
                training.runs.c.status,
                training.runs.c.failure_code,
                training.runs.c.lease_token,
                training.runs.c.attempt_generation,
            ).where(training.runs.c.id == training_id)
        ).one()
    assert row == ("FAILED", "TRAINING_CLEANUP_FAILED", None, 1)
    assert (
        training._claim(
            engine, training_id, SimpleNamespace(training_lease_seconds=300)
        )
        is None
    )
    engine.dispose()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process groups")
def test_group_verification_failure_skips_artifact_and_temp_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    class Client:
        def delete_object(self, **_kwargs):
            pytest.fail("artifact cleanup ran after failed group verification")

    training_id, project_id, token = uuid4(), uuid4(), uuid4()
    claim = Claim(
        {"id": training_id, "project_id": project_id},
        token,
        1,
        f"annotations/{project_id}/trainings/{training_id}/working/1-{token}/",
    )
    root = training._training_root(tmp_path, claim)
    root.mkdir(parents=True)
    marker = root / "trainer-file"
    marker.write_text("keep", encoding="ascii")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(training, "_group_alive", lambda _group_id: True)

            def forbid_temp_cleanup(*_args, **_kwargs):
                pytest.fail("temp cleanup ran after failed group verification")

            patch.setattr(training.shutil, "rmtree", forbid_temp_cleanup)
            with pytest.raises(
                TrainingError, match="Training process cleanup failure"
            ) as error:
                training._cleanup_training(
                    process,
                    Client(),
                    "private",
                    claim,
                    [f"annotations/{project_id}/trainings/{training_id}/model.pt"],
                    [claim.prefix + "snapshot.zip"],
                    root,
                )
            assert error.value.__cause__ is None
            assert marker.read_text(encoding="ascii") == "keep"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("exit_path", ["soft-timeout", "cancellation"])
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process groups")
def test_terminate_kills_exact_group_and_preserves_sentinel(
    tmp_path: Path,
    exit_path: str,
) -> None:
    child_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess,sys,time,pathlib;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid));"
        "time.sleep(60)"
    )
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        grandchild = int(child_file.read_text())
        _terminate(process)
        # Workspace cleanup is deliberately after process-tree termination.
        cleanup_marker = tmp_path / f"{exit_path}.cleanup"
        cleanup_marker.write_text("clean", encoding="ascii")
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process.poll() is not None
        assert not _alive(grandchild)
        assert sentinel.poll() is None
        assert cleanup_marker.read_text(encoding="ascii") == "clean"
    finally:
        if process.poll() is None:
            process.kill()
        sentinel.kill()
        sentinel.wait(timeout=5)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process groups")
def test_terminate_cleans_grandchild_after_trainer_exits(tmp_path: Path) -> None:
    child_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess,sys,pathlib;"
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(60)']);"
        f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid))"
    )
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        process.wait(timeout=5)
        grandchild = int(child_file.read_text())
        assert _alive(grandchild)
        _terminate(process)
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(grandchild)
    finally:
        if child_file.exists():
            try:
                os.kill(int(child_file.read_text()), 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux prctl")
def test_parent_hard_kill_terminates_trainer_and_grandchild(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    child_code = (
        "import os,subprocess,sys,time,pathlib;"
        "from frame_worker.orchestration.yolo_trainer import "
        "install_parent_death_protection;"
        "expected=int(sys.argv[1]);install_parent_death_protection(expected);"
        "g=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(60)']);"
        f"pathlib.Path({str(pids)!r}).write_text(str(os.getpid())+' '+str(g.pid));"
        "time.sleep(60)"
    )
    parent_code = (
        "import os,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r},str(os.getpid())],"
        "start_new_session=True);time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        deadline = time.monotonic() + 5
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        trainer, grandchild = map(int, pids.read_text().split())
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while (_alive(trainer) or _alive(grandchild)) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(trainer)
        assert not _alive(grandchild)
        assert sentinel.poll() is None
    finally:
        if parent.poll() is None:
            parent.kill()
        if sentinel.poll() is None:
            sentinel.kill()
        sentinel.wait(timeout=5)
