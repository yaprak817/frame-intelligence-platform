import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import cv2
import numpy as np


class FFmpegConfigurationError(RuntimeError):
    """Raised when the FFmpeg executables cannot be resolved."""


class FFmpegExtractionError(RuntimeError):
    """Raised when FFmpeg or ffprobe cannot process a video."""


@dataclass(frozen=True)
class VideoMetadata:
    source_fps: float
    total_frames: int
    duration_seconds: float


@dataclass(frozen=True)
class ExtractedFrame:
    frame: np.ndarray
    frame_number: int
    timestamp: float


class ExtractionResult(AbstractContextManager["ExtractionResult"]):
    def __init__(
        self,
        metadata: VideoMetadata,
        directory: Path,
        temporary_directory: tempfile.TemporaryDirectory[str],
        candidate_fps: float,
    ) -> None:
        self.metadata = metadata
        self._directory = directory
        self._temporary_directory = temporary_directory
        self._candidate_fps = candidate_fps

    def __iter__(self) -> Iterator[ExtractedFrame]:
        for index, frame_path in enumerate(
            sorted(self._directory.glob("candidate_*.png")),
            start=1,
        ):
            frame = cv2.imread(str(frame_path), cv2.IMREAD_COLOR)
            if frame is None:
                raise RuntimeError(f"Could not read extracted frame: {frame_path}")
            yield ExtractedFrame(
                frame=frame,
                frame_number=index,
                timestamp=(index - 1) / self._candidate_fps,
            )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._temporary_directory.cleanup()


def _resolve_binary(explicit: str | Path | None, environment_name: str) -> str:
    configured = explicit or os.environ.get(environment_name)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        raise FFmpegConfigurationError(
            f"{environment_name} does not point to an executable file: {path}"
        )

    executable_name = "ffprobe" if environment_name == "FFPROBE_BINARY" else "ffmpeg"
    resolved = shutil.which(executable_name)
    if resolved:
        return resolved

    raise FFmpegConfigurationError(
        f"Could not find {executable_name}. Set {environment_name} to its "
        "executable path "
        f"or add {executable_name} to PATH."
    )


def _parse_fps(value: str) -> float:
    numerator, separator, denominator = value.partition("/")
    if separator:
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0
    return float(value)


def _raise_extraction_error(
    tool_name: str,
    error: subprocess.CalledProcessError,
) -> None:
    stderr = error.stderr
    if isinstance(stderr, bytes):
        stderr_text = stderr.decode("utf-8", errors="replace")
    else:
        stderr_text = stderr or "No stderr output was produced."

    raise FFmpegExtractionError(
        f"{tool_name} failed with exit code {error.returncode}: "
        f"{stderr_text.strip()}"
    ) from error


class FFmpegExtractor:
    """Extract candidate frames at a fixed rate using FFmpeg."""

    def __init__(
        self,
        ffmpeg_binary: str | Path | None = None,
        ffprobe_binary: str | Path | None = None,
    ) -> None:
        self.ffmpeg_binary = _resolve_binary(ffmpeg_binary, "FFMPEG_BINARY")
        self.ffprobe_binary = _resolve_binary(ffprobe_binary, "FFPROBE_BINARY")

    def probe(self, video_path: Path) -> VideoMetadata:
        command = [
            self.ffprobe_binary,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,nb_frames,duration:format=duration",
            "-of",
            "json",
            str(video_path),
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except subprocess.CalledProcessError as error:
            _raise_extraction_error("ffprobe", error)
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        if not streams:
            raise ValueError(f"No video stream found: {video_path}")

        stream = streams[0]
        source_fps = _parse_fps(stream.get("avg_frame_rate", "0"))
        if source_fps <= 0:
            raise ValueError("Video FPS must be greater than zero")

        duration_raw = stream.get("duration") or payload.get("format", {}).get(
            "duration", 0
        )
        duration_seconds = float(duration_raw or 0)
        frames_raw = stream.get("nb_frames")
        total_frames = (
            int(frames_raw)
            if frames_raw not in (None, "N/A")
            else int(round(duration_seconds * source_fps))
        )
        if duration_seconds <= 0 and total_frames > 0:
            duration_seconds = total_frames / source_fps

        return VideoMetadata(source_fps, total_frames, duration_seconds)

    def extract(self, video_path: Path, candidate_fps: float) -> ExtractionResult:
        if candidate_fps <= 0:
            raise ValueError("candidate_fps must be greater than zero")

        metadata = self.probe(video_path)
        temporary_directory = tempfile.TemporaryDirectory(prefix="frame-worker-")
        directory = Path(temporary_directory.name)
        output_pattern = directory / "candidate_%09d.png"
        command = [
            self.ffmpeg_binary,
            "-v",
            "error",
            "-i",
            str(video_path),
            "-vf",
            f"fps={candidate_fps}",
            str(output_pattern),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True)
        except subprocess.CalledProcessError as error:
            temporary_directory.cleanup()
            _raise_extraction_error("FFmpeg", error)
        except BaseException:
            temporary_directory.cleanup()
            raise

        return ExtractionResult(
            metadata=metadata,
            directory=directory,
            temporary_directory=temporary_directory,
            candidate_fps=candidate_fps,
        )
