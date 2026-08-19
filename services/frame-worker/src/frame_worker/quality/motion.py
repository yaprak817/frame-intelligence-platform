import cv2
import numpy as np


def calculate_motion(
    previous_frame: np.ndarray | None,
    current_frame: np.ndarray,
) -> float:
    """Estimate motion using Farneback optical flow."""

    if previous_frame is None:
        return 0.0

    previous_gray = cv2.cvtColor(
        previous_frame,
        cv2.COLOR_BGR2GRAY,
    )

    current_gray = cv2.cvtColor(
        current_frame,
        cv2.COLOR_BGR2GRAY,
    )

    previous_gray = cv2.resize(
        previous_gray,
        (320, 180),
        interpolation=cv2.INTER_AREA,
    )

    current_gray = cv2.resize(
        current_gray,
        (320, 180),
        interpolation=cv2.INTER_AREA,
    )

    flow = cv2.calcOpticalFlowFarneback(
        previous_gray,
        current_gray,
        None,
        0.5,
        3,
        15,
        3,
        5,
        1.2,
        0,
    )

    magnitude, _ = cv2.cartToPolar(
        flow[..., 0],
        flow[..., 1],
    )

    magnitude = np.clip(
        magnitude,
        0,
        20,
    )

    return float(np.mean(magnitude))
