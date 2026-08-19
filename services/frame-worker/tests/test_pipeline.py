import numpy as np
import pytest

import frame_worker.processing.pipeline as pipeline_module
import frame_worker.quality.motion as motion_module
from frame_worker.extraction.ffmpeg import ExtractedFrame, VideoMetadata
from frame_worker.processing.pipeline import (
    ProcessingConfig,
    VideoProcessor,
)
from frame_worker.quality.scorer import QualityResult


class FakeExtraction:
    def __init__(
        self,
        frames: list[np.ndarray],
        fps: float,
        total_frames: int | None = None,
    ) -> None:
        self.frames = frames
        source_frame_count = total_frames or len(frames)
        duration = source_frame_count / fps if fps > 0 else 0.0
        self.metadata = VideoMetadata(fps, source_frame_count, duration)
        self.closed = False

    def __iter__(self):
        for index, frame in enumerate(self.frames):
            yield ExtractedFrame(frame, index + 1, index / 5.0)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.closed = True


class FakeExtractor:
    def __init__(self, extraction: FakeExtraction) -> None:
        self.extraction = extraction
        self.calls = []

    def extract(self, video_path, candidate_fps):
        self.calls.append((video_path, candidate_fps))
        return self.extraction


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
) -> None:
    frames = make_test_frames(10)

    extraction = FakeExtraction(
        frames=frames,
        fps=25.0,
        total_frames=50,
    )
    extractor = FakeExtractor(extraction)

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video")

    output_directory = tmp_path / "output"

    processor = VideoProcessor(
        ProcessingConfig(
            candidate_fps=5.0,
            selection_window_seconds=1.0,
            min_size=64,
        ),
        extractor=extractor,
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
    assert summary.shortlisted_frames == 6
    assert summary.selected_frames == 2
    assert summary.duplicate_frames == 0

    assert summary.processing_seconds >= 0
    assert summary.speed_x is not None
    assert len(saved_files) == 2
    assert extraction.closed is True


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
) -> None:
    extraction = FakeExtraction(
        frames=make_test_frames(10),
        fps=0.0,
    )

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video")

    processor = VideoProcessor(extractor=FakeExtractor(extraction))

    with pytest.raises(ValueError, match="Video FPS must be greater than zero"):
        processor.process(
            video_path=video_path,
            output_directory=tmp_path / "output",
        )

    assert extraction.closed is True


def test_optical_flow_quality_runs_only_for_shortlisted_frames(
    tmp_path,
    monkeypatch,
) -> None:
    frames = make_test_frames(10)
    extraction = FakeExtraction(frames=frames, fps=5.0)
    processor = VideoProcessor(
        ProcessingConfig(candidate_fps=5.0, shortlist_size=2, min_size=64),
        extractor=FakeExtractor(extraction),
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video")
    original = pipeline_module.calculate_quality
    calls = []

    def tracked_quality(frame, previous_frame=None):
        calls.append(frame)
        return original(frame, previous_frame)

    monkeypatch.setattr(pipeline_module, "calculate_quality", tracked_quality)

    summary = processor.process(video_path, tmp_path / "output")

    assert summary.candidate_frames == 10
    assert summary.shortlisted_frames == 4
    assert len(calls) == 4


def run_adaptive_motion_count(
    tmp_path,
    monkeypatch,
    scores: list[float],
) -> tuple[int, int]:
    frames = [
        np.full((120, 160, 3), index, dtype=np.uint8)
        for index in range(len(scores))
    ]
    extraction = FakeExtraction(frames=frames, fps=5.0)
    processor = VideoProcessor(
        ProcessingConfig(
            candidate_fps=5.0,
            shortlist_size=3,
            adaptive_shortlist_enabled=True,
            adaptive_shortlist_threshold=0.04,
            min_size=64,
        ),
        extractor=FakeExtractor(extraction),
    )
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video")

    def fake_fast_quality(frame):
        score = scores[int(frame[0, 0, 0])]
        return QualityResult(score, score, 128.0, 1.0, 0.0)

    original_farneback = motion_module.cv2.calcOpticalFlowFarneback
    farneback_calls = 0

    def tracked_farneback(*args, **kwargs):
        nonlocal farneback_calls
        farneback_calls += 1
        return original_farneback(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "calculate_fast_quality", fake_fast_quality)
    monkeypatch.setattr(
        motion_module.cv2,
        "calcOpticalFlowFarneback",
        tracked_farneback,
    )
    summary = processor.process(video_path, tmp_path / "output")
    return summary.shortlisted_frames, farneback_calls


def test_adaptive_top_two_window_runs_farneback_once(
    tmp_path,
    monkeypatch,
) -> None:
    shortlist, farneback_calls = run_adaptive_motion_count(
        tmp_path,
        monkeypatch,
        [100.0, 90.0, 10.0],
    )

    assert shortlist == 2
    assert farneback_calls == 1


def test_adaptive_top_three_window_preserves_motion_reranking(
    tmp_path,
    monkeypatch,
) -> None:
    shortlist, farneback_calls = run_adaptive_motion_count(
        tmp_path,
        monkeypatch,
        [100.0, 90.0, 88.0],
    )

    assert shortlist == 3
    assert farneback_calls == 2
