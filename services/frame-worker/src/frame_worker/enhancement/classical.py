import cv2
import numpy as np

DEFAULT_MIN_SIZE = 640


def ensure_min_size(
    frame: np.ndarray,
    min_size: int = DEFAULT_MIN_SIZE,
) -> np.ndarray:
    """Ensure both frame dimensions satisfy the configured minimum size."""

    if min_size <= 0:
        raise ValueError("min_size must be greater than zero")

    height, width = frame.shape[:2]

    if height >= min_size and width >= min_size:
        return frame.copy()

    scale = max(
        min_size / width,
        min_size / height,
    )

    new_width = int(round(width * scale))
    new_height = int(round(height * scale))

    return cv2.resize(
        frame,
        (new_width, new_height),
        interpolation=cv2.INTER_LANCZOS4,
    )


def enhance_frame(
    frame: np.ndarray,
    min_size: int = DEFAULT_MIN_SIZE,
) -> np.ndarray:
    """Apply classical resizing and lightweight sharpening."""

    image = ensure_min_size(
        frame,
        min_size=min_size,
    )

    blurred = cv2.GaussianBlur(
        image,
        (0, 0),
        sigmaX=1.0,
    )

    return cv2.addWeighted(
        image,
        1.15,
        blurred,
        -0.15,
        0,
    )
