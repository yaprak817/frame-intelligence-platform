import numpy as np

from frame_worker.quality.exposure import (
    calculate_brightness,
    calculate_exposure_score,
)
from frame_worker.quality.motion import calculate_motion
from frame_worker.quality.scorer import calculate_quality
from frame_worker.quality.sharpness import calculate_sharpness


def test_black_frame_has_zero_brightness() -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    assert calculate_brightness(frame) == 0.0


def test_mid_gray_has_high_exposure_score() -> None:
    assert calculate_exposure_score(128.0) == 1.0


def test_first_frame_has_zero_motion() -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    assert calculate_motion(None, frame) == 0.0


def test_uniform_frame_has_zero_sharpness() -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    assert calculate_sharpness(frame) == 0.0


def test_quality_result_contains_metrics() -> None:
    frame = np.full(
        (720, 1280, 3),
        128,
        dtype=np.uint8,
    )

    result = calculate_quality(frame)

    assert result.brightness == 128.0
    assert result.exposure == 1.0
    assert result.motion == 0.0
    assert result.quality >= 0.0
