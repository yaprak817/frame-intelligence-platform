from dataclasses import dataclass

import numpy as np

from frame_worker.quality.exposure import (
    calculate_brightness,
    calculate_exposure_score,
)
from frame_worker.quality.motion import calculate_motion
from frame_worker.quality.sharpness import calculate_sharpness

MIN_BRIGHTNESS = 15
MAX_BRIGHTNESS = 245
MOTION_WEIGHT = 0.08


@dataclass(frozen=True)
class QualityResult:
    quality: float
    sharpness: float
    brightness: float
    exposure: float
    motion: float


def calculate_quality(
    frame: np.ndarray,
    previous_frame: np.ndarray | None = None,
) -> QualityResult:
    """Calculate combined frame quality metrics."""

    sharpness = calculate_sharpness(frame)
    brightness = calculate_brightness(frame)
    exposure = calculate_exposure_score(brightness)
    motion = calculate_motion(previous_frame, frame)

    quality = sharpness * exposure / (1.0 + MOTION_WEIGHT * motion)

    if brightness < MIN_BRIGHTNESS or brightness > MAX_BRIGHTNESS:
        quality *= 0.25

    return QualityResult(
        quality=float(quality),
        sharpness=float(sharpness),
        brightness=float(brightness),
        exposure=float(exposure),
        motion=float(motion),
    )


def calculate_fast_quality(frame: np.ndarray) -> QualityResult:
    """Calculate inexpensive metrics without optical flow."""

    sharpness = calculate_sharpness(frame)
    brightness = calculate_brightness(frame)
    exposure = calculate_exposure_score(brightness)
    quality = sharpness * exposure

    if brightness < MIN_BRIGHTNESS or brightness > MAX_BRIGHTNESS:
        quality *= 0.25

    return QualityResult(
        quality=float(quality),
        sharpness=float(sharpness),
        brightness=float(brightness),
        exposure=float(exposure),
        motion=0.0,
    )
