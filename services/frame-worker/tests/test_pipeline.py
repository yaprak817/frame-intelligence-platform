import cv2
import numpy as np
import pytest

import frame_worker.processing.pipeline as pipeline_module
from frame_worker.processing.pipeline import (
    ProcessingConfig,
    VideoProcessor,
)


class FakeCapture:
    def __init__(
        self,
        frames: list[np.ndarray],
        fps: float,
    ) -> None:
        self.frames = frames
        self.fps = fps
        self.index = 0
        self.released = False

    def isOpened(self) -> bool:
        return True

    def get(self, property_id: int) -> float:
        if property_id == cv2.CAP_PROP_FPS:
            return self.fps

        if property_id == cv2.CAP_PROP_FRAME_COUNT:
            return float(len(self.frames))

        return 0.0

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.index >= len(self.frames):
            return False, None

        frame = self.frames[self.index]
        self.index += 1

        return True, frame.copy()

    def release(self) -> None:
        self.released = True


def make_test_frames(
    count: int,
) -> list[np.ndarray]:
    frames = []

    for index in range(count):
        rng = np.random.default_rng(index)

        frame = rng.integers(
            0,
            256,
            size=(120, 160, 3),
            dtype=np.uint8,
        )

        frames.append(frame)

    return frames


def test_processor_samples_and_saves_frames(
    tmp_path,
    monkeypatch,
) -> None:
    frames = make_test_frames(50)

    fake_capture = FakeCapture(
        frames=frames,
        fps=25.0,
    )

    monkeypatch.setattr(
        pipeline_module.cv2,
        "VideoCapture",
        lambda _: fake_capture,
    )

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video")

    output_directory = tmp_path / "output"

    processor = VideoProcessor(
        ProcessingConfig(
            candidate_fps=5.0,
            selection_window_seconds=1.0,
            min_size=64,
        )
    )

    summary = processor.process(
        video_path=video_path,
        output_directory=output_directory,
    )

    saved_files = list(output_directory.glob("*.jpg"))

    assert summary.source_fps == 25.0
    assert summary.total_frames == 50
    assert summary.duration_seconds == 2.0

    assert summary.candidate_frames == 10
    assert summary.selected_frames == 3
    assert summary.duplicate_frames == 0

    assert len(saved_files) == 3
    assert fake_capture.released is True


def test_missing_video_raises_error(
    tmp_path,
) -> None:
    processor = VideoProcessor()

    video_path = tmp_path / "missing.mp4"
    output_directory = tmp_path / "output"

    with pytest.raises(FileNotFoundError):
        processor.process(
            video_path=video_path,
            output_directory=output_directory,
        )


def test_invalid_video_fps_raises_error(
    tmp_path,
    monkeypatch,
) -> None:
    fake_capture = FakeCapture(
        frames=make_test_frames(10),
        fps=0.0,
    )

    monkeypatch.setattr(
        pipeline_module.cv2,
        "VideoCapture",
        lambda _: fake_capture,
    )

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video")

    processor = VideoProcessor()

    with pytest.raises(
        ValueError,
        match="Video FPS must be greater than zero",
    ):
        processor.process(
            video_path=video_path,
            output_directory=tmp_path / "output",
        )

    assert fake_capture.released is True
