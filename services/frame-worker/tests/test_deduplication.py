import numpy as np

from frame_worker.deduplication.ssim import (
    calculate_similarity,
    is_near_duplicate,
)


def test_identical_frames_have_max_similarity() -> None:
    frame = np.full(
        (720, 1280, 3),
        128,
        dtype=np.uint8,
    )

    similarity = calculate_similarity(
        frame,
        frame.copy(),
    )

    assert similarity == 1.0


def test_first_frame_is_not_duplicate() -> None:
    frame = np.zeros(
        (720, 1280, 3),
        dtype=np.uint8,
    )

    result = is_near_duplicate(
        current_frame=frame,
        previous_frame=None,
        current_time=1.0,
        previous_time=None,
    )

    assert result is False


def test_identical_frames_within_time_gap_are_duplicates() -> None:
    frame = np.full(
        (720, 1280, 3),
        128,
        dtype=np.uint8,
    )

    result = is_near_duplicate(
        current_frame=frame.copy(),
        previous_frame=frame,
        current_time=1.5,
        previous_time=1.0,
    )

    assert result is True


def test_identical_frames_outside_time_gap_are_not_duplicates() -> None:
    frame = np.full(
        (720, 1280, 3),
        128,
        dtype=np.uint8,
    )

    result = is_near_duplicate(
        current_frame=frame.copy(),
        previous_frame=frame,
        current_time=4.0,
        previous_time=1.0,
    )

    assert result is False
