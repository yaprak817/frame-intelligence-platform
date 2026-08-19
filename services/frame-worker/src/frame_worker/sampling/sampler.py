from dataclasses import dataclass


@dataclass(frozen=True)
class SamplingConfig:
    candidate_fps: float = 5.0
    selection_window_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.candidate_fps <= 0:
            raise ValueError("candidate_fps must be greater than zero")

        if self.selection_window_seconds <= 0:
            raise ValueError("selection_window_seconds must be greater than zero")


def calculate_frame_interval(
    source_fps: float,
    candidate_fps: float,
) -> int:
    """Calculate source-frame interval for candidate sampling."""

    if source_fps <= 0:
        raise ValueError("source_fps must be greater than zero")

    if candidate_fps <= 0:
        raise ValueError("candidate_fps must be greater than zero")

    return max(
        1,
        int(round(source_fps / candidate_fps)),
    )


def should_sample_frame(
    frame_number: int,
    frame_interval: int,
) -> bool:
    """Return whether the source frame should be analyzed."""

    if frame_number < 0:
        raise ValueError("frame_number cannot be negative")

    if frame_interval <= 0:
        raise ValueError("frame_interval must be greater than zero")

    return frame_number % frame_interval == 0


def calculate_timestamp(
    frame_number: int,
    source_fps: float,
) -> float:
    """Calculate frame timestamp in seconds."""

    if frame_number < 0:
        raise ValueError("frame_number cannot be negative")

    if source_fps <= 0:
        raise ValueError("source_fps must be greater than zero")

    return frame_number / source_fps


def calculate_window_index(
    timestamp: float,
    window_seconds: float,
) -> int:
    """Return the selection-window index for a timestamp."""

    if timestamp < 0:
        raise ValueError("timestamp cannot be negative")

    if window_seconds <= 0:
        raise ValueError("window_seconds must be greater than zero")

    return int(timestamp // window_seconds)
