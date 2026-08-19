import cv2
import numpy as np


def calculate_sharpness(frame: np.ndarray) -> float:
    """Calculate frame sharpness using Laplacian variance."""

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    height, width = gray.shape

    if width > 640:
        scale = 640 / width

        gray = cv2.resize(
            gray,
            (
                640,
                int(height * scale),
            ),
            interpolation=cv2.INTER_AREA,
        )

    laplacian = cv2.Laplacian(
        gray,
        cv2.CV_64F,
    )

    return float(laplacian.var())
