import pytest

from frame_worker.sampling.sampler import (
    SamplingConfig,
    calculate_frame_interval,
    calculate_timestamp,
    calculate_window_index,
    should_sample_frame,
)


def test_25_fps_with_5_candidate_fps_has_interval_5() -> None:
    result = calculate_frame_interval(
        source_fps=25.0,
        candidate_fps=5.0,
    )

    assert result == 5


def test_candidate_fps_above_source_fps_uses_every_frame() -> None:
    result = calculate_frame_interval(
        source_fps=25.0,
        candidate_fps=50.0,
    )

    assert result == 1


def test_every_fifth_frame_is_sampled() -> None:
    assert should_sample_frame(5, 5) is True
    assert should_sample_frame(10, 5) is True
    assert should_sample_frame(6, 5) is False


def test_timestamp_calculation() -> None:
    result = calculate_timestamp(
        frame_number=25,
        source_fps=25.0,
    )

    assert result == 1.0


def test_timestamp_maps_to_correct_window() -> None:
    assert calculate_window_index(0.2, 1.0) == 0
    assert calculate_window_index(0.9, 1.0) == 0
    assert calculate_window_index(1.0, 1.0) == 1
    assert calculate_window_index(2.7, 1.0) == 2


def test_invalid_candidate_fps_raises_error() -> None:
    with pytest.raises(ValueError):
        SamplingConfig(candidate_fps=0)


def test_invalid_source_fps_raises_error() -> None:
    with pytest.raises(ValueError):
        calculate_frame_interval(
            source_fps=0,
            candidate_fps=5,
        )
