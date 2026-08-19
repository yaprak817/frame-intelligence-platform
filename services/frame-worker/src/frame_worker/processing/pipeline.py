from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

from frame_worker.deduplication.ssim import is_near_duplicate
from frame_worker.enhancement.classical import enhance_frame
from frame_worker.extraction.ffmpeg import (
    ExtractedFrame,
    FFmpegExtractor,
    StreamingFFmpegExtractor,
)
from frame_worker.processing.selector import FrameCandidate, ShortlistCandidateSelector
from frame_worker.quality.scorer import calculate_fast_quality, calculate_quality

DEFAULT_JPEG_QUALITY = 98
DEFAULT_SHORTLIST_SIZE = 3
DEFAULT_ADAPTIVE_SHORTLIST_THRESHOLD = 0.04


@dataclass(frozen=True)
class ProcessingConfig:
    candidate_fps: float = 5.0
    selection_window_seconds: float = 1.0
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE
    adaptive_shortlist_enabled: bool = False
    adaptive_shortlist_threshold: float = DEFAULT_ADAPTIVE_SHORTLIST_THRESHOLD
    min_size: int = 640
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    ffmpeg_binary: str | Path | None = None
    ffprobe_binary: str | Path | None = None

    def __post_init__(self) -> None:
        if self.candidate_fps <= 0:
            raise ValueError("candidate_fps must be greater than zero")
        if self.selection_window_seconds <= 0:
            raise ValueError("selection_window_seconds must be greater than zero")
        if self.shortlist_size <= 0:
            raise ValueError("shortlist_size must be greater than zero")
        if self.adaptive_shortlist_enabled and self.shortlist_size < 3:
            raise ValueError("adaptive shortlist requires shortlist_size of at least 3")
        if self.adaptive_shortlist_threshold < 0:
            raise ValueError("adaptive_shortlist_threshold cannot be negative")


@dataclass(frozen=True)
class ProcessingSummary:
    source_fps: float
    total_frames: int
    duration_seconds: float
    candidate_frames: int
    shortlisted_frames: int
    selected_frames: int
    duplicate_frames: int
    processing_seconds: float
    output_directory: Path

    @property
    def speed_x(self) -> float | None:
        """Return processed video seconds per wall-clock second."""

        if self.processing_seconds <= 0:
            return None
        return self.duration_seconds / self.processing_seconds


@dataclass
class _ProcessingCounts:
    candidates: int = 0
    shortlisted: int = 0
    selected: int = 0
    duplicates: int = 0


class VideoProcessor:
    """Process a local video and extract high-quality representative frames."""

    def __init__(
        self,
        config: ProcessingConfig | None = None,
        extractor: FFmpegExtractor | None = None,
    ) -> None:
        self.config = config or ProcessingConfig()
        self._extractor = extractor

    def process(self, video_path: Path, output_directory: Path) -> ProcessingSummary:
        started_at = perf_counter()
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        output_directory.mkdir(parents=True, exist_ok=True)
        extractor = self._extractor or StreamingFFmpegExtractor(
            ffmpeg_binary=self.config.ffmpeg_binary,
            ffprobe_binary=self.config.ffprobe_binary,
        )
        with extractor.extract(video_path, self.config.candidate_fps) as extraction:
            if extraction.metadata.source_fps <= 0:
                raise ValueError("Video FPS must be greater than zero")
            counts = self._process_candidates(extraction, output_directory)
            metadata = extraction.metadata

        return ProcessingSummary(
            source_fps=metadata.source_fps,
            total_frames=metadata.total_frames,
            duration_seconds=metadata.duration_seconds,
            candidate_frames=counts.candidates,
            shortlisted_frames=counts.shortlisted,
            selected_frames=counts.selected,
            duplicate_frames=counts.duplicates,
            processing_seconds=perf_counter() - started_at,
            output_directory=output_directory,
        )

    def _process_candidates(
        self,
        extracted_frames: Iterable[ExtractedFrame],
        output_directory: Path,
    ) -> _ProcessingCounts:
        selector = ShortlistCandidateSelector(
            window_seconds=self.config.selection_window_seconds,
            shortlist_size=self.config.shortlist_size,
            adaptive_enabled=self.config.adaptive_shortlist_enabled,
            adaptive_threshold=self.config.adaptive_shortlist_threshold,
        )
        counts = _ProcessingCounts()
        last_saved_frame: np.ndarray | None = None
        last_saved_time: float | None = None

        for extracted in extracted_frames:
            counts.candidates += 1
            candidate = FrameCandidate(
                frame=extracted.frame.copy(),
                frame_number=extracted.frame_number,
                timestamp=extracted.timestamp,
                quality=calculate_fast_quality(extracted.frame),
            )
            completed = selector.add(candidate)
            if completed is not None:
                last_saved_frame, last_saved_time = self._finish_window(
                    completed,
                    output_directory,
                    counts,
                    last_saved_frame,
                    last_saved_time,
                )

        final_shortlist = selector.flush()
        if final_shortlist:
            self._finish_window(
                final_shortlist,
                output_directory,
                counts,
                last_saved_frame,
                last_saved_time,
            )
        return counts

    def _finish_window(
        self,
        shortlist: list[FrameCandidate],
        output_directory: Path,
        counts: _ProcessingCounts,
        last_saved_frame: np.ndarray | None,
        last_saved_time: float | None,
    ) -> tuple[np.ndarray | None, float | None]:
        counts.shortlisted += len(shortlist)
        previous_shortlisted_frame: np.ndarray | None = None
        evaluated: list[FrameCandidate] = []
        for candidate in sorted(shortlist, key=lambda item: item.timestamp):
            quality = calculate_quality(candidate.frame, previous_shortlisted_frame)
            evaluated.append(
                FrameCandidate(
                    frame=candidate.frame,
                    frame_number=candidate.frame_number,
                    timestamp=candidate.timestamp,
                    quality=quality,
                )
            )
            previous_shortlisted_frame = candidate.frame

        best = max(evaluated, key=lambda item: item.quality.quality)
        saved, duplicate, saved_frame, saved_time = self._save_candidate(
            candidate=best,
            output_directory=output_directory,
            selected_count=counts.selected,
            last_saved_frame=last_saved_frame,
            last_saved_time=last_saved_time,
        )
        counts.selected += int(saved)
        counts.duplicates += int(duplicate)
        return saved_frame, saved_time

    def _save_candidate(
        self,
        candidate: FrameCandidate,
        output_directory: Path,
        selected_count: int,
        last_saved_frame: np.ndarray | None,
        last_saved_time: float | None,
    ) -> tuple[bool, bool, np.ndarray | None, float | None]:
        duplicate = is_near_duplicate(
            current_frame=candidate.frame,
            previous_frame=last_saved_frame,
            current_time=candidate.timestamp,
            previous_time=last_saved_time,
        )
        if duplicate:
            return False, True, last_saved_frame, last_saved_time

        enhanced = enhance_frame(candidate.frame, min_size=self.config.min_size)
        timestamp_ms = int(candidate.timestamp * 1000)
        height, width = enhanced.shape[:2]
        filename = f"frame_{selected_count:06d}_{timestamp_ms}ms_{width}x{height}.jpg"
        output_path = output_directory / filename
        success, encoded = cv2.imencode(
            ".jpg", enhanced, [cv2.IMWRITE_JPEG_QUALITY, self.config.jpeg_quality]
        )
        if not success:
            raise RuntimeError(f"Could not encode frame: {filename}")
        encoded.tofile(output_path)
        return True, False, candidate.frame.copy(), candidate.timestamp
