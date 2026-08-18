from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from frame_worker.deduplication.ssim import is_near_duplicate
from frame_worker.enhancement.classical import enhance_frame
from frame_worker.processing.selector import (
    BestCandidateSelector,
    FrameCandidate,
)
from frame_worker.quality.scorer import calculate_quality
from frame_worker.sampling.sampler import (
    SamplingConfig,
    calculate_frame_interval,
    calculate_timestamp,
    should_sample_frame,
)

DEFAULT_JPEG_QUALITY = 98


@dataclass(frozen=True)
class ProcessingConfig:
    candidate_fps: float = 5.0
    selection_window_seconds: float = 1.0
    min_size: int = 640
    jpeg_quality: int = DEFAULT_JPEG_QUALITY


@dataclass(frozen=True)
class ProcessingSummary:
    source_fps: float
    total_frames: int
    duration_seconds: float
    candidate_frames: int
    selected_frames: int
    duplicate_frames: int
    output_directory: Path


class VideoProcessor:
    """Process a local video and extract high-quality representative frames."""

    def __init__(
        self,
        config: ProcessingConfig | None = None,
    ) -> None:
        self.config = config or ProcessingConfig()

    def process(
        self,
        video_path: Path,
        output_directory: Path,
    ) -> ProcessingSummary:
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        output_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        capture = cv2.VideoCapture(str(video_path))

        if not capture.isOpened():
            raise ValueError(f"Could not open video: {video_path}")

        try:
            return self._process_capture(
                capture=capture,
                output_directory=output_directory,
            )
        finally:
            capture.release()

    def _process_capture(
        self,
        capture: cv2.VideoCapture,
        output_directory: Path,
    ) -> ProcessingSummary:
        source_fps = float(capture.get(cv2.CAP_PROP_FPS))

        if source_fps <= 0:
            raise ValueError("Video FPS must be greater than zero")

        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))

        duration_seconds = total_frames / source_fps if total_frames > 0 else 0.0

        sampling_config = SamplingConfig(
            candidate_fps=self.config.candidate_fps,
            selection_window_seconds=(self.config.selection_window_seconds),
        )

        frame_interval = calculate_frame_interval(
            source_fps=source_fps,
            candidate_fps=sampling_config.candidate_fps,
        )

        selector = BestCandidateSelector(
            window_seconds=(sampling_config.selection_window_seconds)
        )

        frame_number = 0
        candidate_count = 0
        selected_count = 0
        duplicate_count = 0

        previous_candidate_frame: np.ndarray | None = None
        last_saved_frame: np.ndarray | None = None
        last_saved_time: float | None = None

        while True:
            success, frame = capture.read()

            if not success:
                break

            frame_number += 1

            if not should_sample_frame(
                frame_number,
                frame_interval,
            ):
                continue

            candidate_count += 1

            timestamp = calculate_timestamp(
                frame_number=frame_number,
                source_fps=source_fps,
            )

            quality = calculate_quality(
                frame=frame,
                previous_frame=previous_candidate_frame,
            )

            candidate = FrameCandidate(
                frame=frame.copy(),
                frame_number=frame_number,
                timestamp=timestamp,
                quality=quality,
            )

            completed_candidate = selector.add(candidate)

            if completed_candidate is not None:
                (
                    saved,
                    duplicate,
                    last_saved_frame,
                    last_saved_time,
                ) = self._save_candidate(
                    candidate=completed_candidate,
                    output_directory=output_directory,
                    selected_count=selected_count,
                    last_saved_frame=last_saved_frame,
                    last_saved_time=last_saved_time,
                )

                if saved:
                    selected_count += 1

                if duplicate:
                    duplicate_count += 1

            previous_candidate_frame = frame.copy()

        final_candidate = selector.flush()

        if final_candidate is not None:
            (
                saved,
                duplicate,
                last_saved_frame,
                last_saved_time,
            ) = self._save_candidate(
                candidate=final_candidate,
                output_directory=output_directory,
                selected_count=selected_count,
                last_saved_frame=last_saved_frame,
                last_saved_time=last_saved_time,
            )

            if saved:
                selected_count += 1

            if duplicate:
                duplicate_count += 1

        return ProcessingSummary(
            source_fps=source_fps,
            total_frames=total_frames,
            duration_seconds=duration_seconds,
            candidate_frames=candidate_count,
            selected_frames=selected_count,
            duplicate_frames=duplicate_count,
            output_directory=output_directory,
        )

    def _save_candidate(
        self,
        candidate: FrameCandidate,
        output_directory: Path,
        selected_count: int,
        last_saved_frame: np.ndarray | None,
        last_saved_time: float | None,
    ) -> tuple[
        bool,
        bool,
        np.ndarray | None,
        float | None,
    ]:
        duplicate = is_near_duplicate(
            current_frame=candidate.frame,
            previous_frame=last_saved_frame,
            current_time=candidate.timestamp,
            previous_time=last_saved_time,
        )

        if duplicate:
            return (
                False,
                True,
                last_saved_frame,
                last_saved_time,
            )

        enhanced = enhance_frame(
            candidate.frame,
            min_size=self.config.min_size,
        )

        timestamp_ms = int(candidate.timestamp * 1000)

        height, width = enhanced.shape[:2]

        filename = f"frame_{selected_count:06d}_{timestamp_ms}ms_{width}x{height}.jpg"

        output_path = output_directory / filename

        success, encoded = cv2.imencode(
            ".jpg",
            enhanced,
            [
                cv2.IMWRITE_JPEG_QUALITY,
                self.config.jpeg_quality,
            ],
        )

        if not success:
            raise RuntimeError(f"Could not encode frame: {filename}")

        encoded.tofile(output_path)

        return (
            True,
            False,
            candidate.frame.copy(),
            candidate.timestamp,
        )
