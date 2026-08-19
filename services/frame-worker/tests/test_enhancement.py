import numpy as np
import pytest

from frame_worker.enhancement.classical import (
    enhance_frame,
    ensure_min_size,
)


def test_large_frame_keeps_original_dimensions() -> None:
    frame = np.zeros(
        (720, 1280, 3),
        dtype=np.uint8,
    )

    result = ensure_min_size(frame)

    assert result.shape == frame.shape


def test_small_frame_is_upscaled() -> None:
    frame = np.zeros(
        (360, 640, 3),
        dtype=np.uint8,
    )

    result = ensure_min_size(frame)

    height, width = result.shape[:2]

    assert height >= 640
    assert width >= 640


def test_resize_preserves_aspect_ratio() -> None:
    frame = np.zeros(
        (360, 640, 3),
        dtype=np.uint8,
    )

    result = ensure_min_size(frame)

    original_ratio = 640 / 360
    result_ratio = result.shape[1] / result.shape[0]

    assert result_ratio == pytest.approx(
        original_ratio,
        rel=0.01,
    )


def test_enhancement_preserves_frame_type() -> None:
    frame = np.full(
        (720, 1280, 3),
        128,
        dtype=np.uint8,
    )

    result = enhance_frame(frame)

    assert result.dtype == np.uint8


def test_invalid_min_size_raises_error() -> None:
    frame = np.zeros(
        (100, 100, 3),
        dtype=np.uint8,
    )

    with pytest.raises(ValueError):
        ensure_min_size(
            frame,
            min_size=0,
        )
