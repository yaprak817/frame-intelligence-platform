import ctypes
import json
import os
import signal
import sys
import time
from pathlib import Path
from uuid import UUID


def install_parent_death_protection(expected_parent_pid: int) -> None:
    """Kill this Linux process group if its owning worker disappears."""
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise RuntimeError("Unable to install parent-death protection")

    def terminate_group(_signum, _frame) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        group = os.getpgrp()
        if group != os.getpid():
            raise SystemExit(143)
        os.killpg(group, signal.SIGTERM)
        # The trainer ignores its own TERM while descendants receive it. A
        # short bounded grace is followed by fail-closed group termination,
        # including descendants which ignore TERM.
        time.sleep(0.5)
        os.killpg(group, signal.SIGKILL)

    signal.signal(signal.SIGTERM, terminate_group)
    if os.getppid() != expected_parent_pid:
        terminate_group(signal.SIGTERM, None)


def main() -> int:
    if len(sys.argv) != 2 or str(UUID(sys.argv[1])) != sys.argv[1]:
        return 2
    training_id = UUID(sys.argv[1])
    expected_parent = int(os.environ.get("TRAINING_PARENT_PID", "0"))
    if expected_parent <= 1:
        return 2
    install_parent_death_protection(expected_parent)
    root = (
        Path(
            os.environ.get("PROCESSING_TEMP_ROOT", "/tmp/frame-intelligence")
        ).resolve()
        / "annotation-training"
        / str(training_id)
    )
    launch = json.loads((root / "launch.json").read_text(encoding="ascii"))
    from ultralytics import YOLO

    model = YOLO(str(Path(launch["model"]).resolve()))
    model.train(
        data=str((root / "dataset" / "data.yaml").resolve()),
        epochs=launch["epochs"],
        batch=launch["batch_size"],
        imgsz=640,
        device="cpu",
        workers=0,
        amp=False,
        pretrained=True,
        project=str(root / "runs"),
        name="train",
        exist_ok=True,
        save=True,
        plots=False,
        verbose=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
