import cv2
import numpy as np
from skimage.metrics import structural_similarity

DEFAULT_SSIM_THRESHOLD = 0.95
DEFAULT_MAX_TIME_GAP = 2.0


def calculate_similarity(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
) -> float:
    """Calculate structural similarity between two frames."""

    gray_a = cv2.cvtColor(
        frame_a,
        cv2.COLOR_BGR2GRAY,
    )

    gray_b = cv2.cvtColor(
        frame_b,
        cv2.COLOR_BGR2GRAY,
    )

    gray_a = cv2.resize(
        gray_a,
        (320, 180),
        interpolation=cv2.INTER_AREA,
    )

    gray_b = cv2.resize(
        gray_b,
        (320, 180),
        interpolation=cv2.INTER_AREA,
    )

    score = structural_similarity(
        gray_a,
        gray_b,
    )

    return float(score)


def is_near_duplicate(
    current_frame: np.ndarray,
    previous_frame: np.ndarray | None,
    current_time: float,
    previous_time: float | None,
    threshold: float = DEFAULT_SSIM_THRESHOLD,
    max_time_gap: float = DEFAULT_MAX_TIME_GAP,
) -> bool:
    """Determine whether a frame is a temporal near-duplicate."""

    if previous_frame is None or previous_time is None:
        return False

    time_gap = current_time - previous_time

    if time_gap >= max_time_gap:
        return False

    similarity = calculate_similarity(
        previous_frame,
        current_frame,
    )

    return similarity >= threshold
