import json
import os
import shutil
import subprocess
from pathlib import Path

from frame_worker.ingestion.errors import (
    InvalidVideoSourceError,
    VideoTooLargeError,
)

ALLOWED_VIDEO_SUFFIXES = frozenset(
    {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
)


class VideoFileValidator:
    def __init__(self, ffprobe_binary: str | Path | None = None) -> None:
        configured = ffprobe_binary or os.environ.get("FFPROBE_BINARY")
        if configured:
            path = Path(configured).expanduser()
            if not path.is_file():
                raise InvalidVideoSourceError(
                    f"FFPROBE_BINARY does not point to a file: {path}"
                )
            self.ffprobe_binary = str(path)
        else:
            resolved = shutil.which("ffprobe")
            if not resolved:
                raise InvalidVideoSourceError(
                    "Could not find ffprobe. Set FFPROBE_BINARY or add ffprobe to PATH."
                )
            self.ffprobe_binary = resolved

    def validate(self, path: Path, max_bytes: int) -> None:
        validate_file_basics(path, max_bytes)
        command = [
            self.ffprobe_binary,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,width,height:format=duration",
            "-of",
            "json",
            str(path),
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
            stderr = error.stderr or "No stderr output was produced."
            raise InvalidVideoSourceError(
                f"ffprobe could not validate the video: {stderr.strip()}"
            ) from error

        try:
            payload = json.loads(result.stdout)
            streams = payload.get("streams", [])
            if not streams:
                raise InvalidVideoSourceError("Source does not contain a video stream")
            stream = streams[0]
            width = int(stream.get("width", 0))
            height = int(stream.get("height", 0))
            fps = _parse_fps(stream.get("avg_frame_rate", "0"))
            duration = float(payload.get("format", {}).get("duration", 0) or 0)
        except (TypeError, ValueError) as error:
            raise InvalidVideoSourceError(
                "ffprobe returned invalid video metadata"
            ) from error

        if width <= 0 or height <= 0:
            raise InvalidVideoSourceError("Video dimensions must be greater than zero")
        if fps <= 0:
            raise InvalidVideoSourceError("Video FPS must be greater than zero")
        if duration <= 0:
            raise InvalidVideoSourceError("Video duration must be greater than zero")


def validate_file_basics(path: Path, max_bytes: int) -> None:
    if not path.exists():
        raise InvalidVideoSourceError(f"Video source does not exist: {path}")
    if not path.is_file():
        raise InvalidVideoSourceError(f"Video source is not a regular file: {path}")
    size = path.stat().st_size
    if size == 0:
        raise InvalidVideoSourceError(f"Video source is empty: {path}")
    if size > max_bytes:
        raise VideoTooLargeError(
            f"Video source exceeds the {max_bytes} byte limit"
        )
    if path.suffix.lower() not in ALLOWED_VIDEO_SUFFIXES:
        raise InvalidVideoSourceError(
            f"Unsupported video file suffix: {path.suffix or '<none>'}"
        )


def _parse_fps(value: str) -> float:
    numerator, separator, denominator = value.partition("/")
    if separator:
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0
    return float(value)
