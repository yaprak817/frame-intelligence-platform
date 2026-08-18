import cv2
import numpy as np


def calculate_brightness(frame: np.ndarray) -> float:
    """Calculate average frame brightness."""

    gray = cv2.cvtColor(
        frame,
        cv2.COLOR_BGR2GRAY,
    )

    small = cv2.resize(
        gray,
        (320, 180),
        interpolation=cv2.INTER_AREA,
    )

    return float(np.mean(small))


def calculate_exposure_score(brightness: float) -> float:
    """Convert brightness into an exposure quality score."""

    exposure_score = 1.0 - abs(brightness - 128.0) / 128.0

    return max(
        0.1,
        exposure_score,
    )
