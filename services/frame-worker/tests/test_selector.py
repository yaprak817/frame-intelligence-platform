import numpy as np
import pytest

from frame_worker.processing.selector import (
    BestCandidateSelector,
    FrameCandidate,
)
from frame_worker.quality.scorer import QualityResult


def make_candidate(
    timestamp: float,
    quality_score: float,
    frame_number: int = 1,
) -> FrameCandidate:
    frame = np.zeros(
        (100, 100, 3),
        dtype=np.uint8,
    )

    quality = QualityResult(
        quality=quality_score,
        sharpness=quality_score,
        brightness=128.0,
        exposure=1.0,
        motion=0.0,
    )

    return FrameCandidate(
        frame=frame,
        frame_number=frame_number,
        timestamp=timestamp,
        quality=quality,
    )


def test_selector_keeps_highest_quality_in_same_window() -> None:
    selector = BestCandidateSelector(window_seconds=1.0)

    selector.add(make_candidate(0.2, 10.0))
    selector.add(make_candidate(0.4, 50.0))
    selector.add(make_candidate(0.8, 20.0))

    result = selector.flush()

    assert result is not None
    assert result.quality.quality == 50.0


def test_new_window_returns_previous_best_candidate() -> None:
    selector = BestCandidateSelector(window_seconds=1.0)

    selector.add(make_candidate(0.2, 10.0))
    selector.add(make_candidate(0.7, 80.0))

    result = selector.add(make_candidate(1.2, 30.0))

    assert result is not None
    assert result.quality.quality == 80.0


def test_flush_returns_last_window_candidate() -> None:
    selector = BestCandidateSelector()

    selector.add(make_candidate(2.2, 42.0))

    result = selector.flush()

    assert result is not None
    assert result.quality.quality == 42.0


def test_flush_empty_selector_returns_none() -> None:
    selector = BestCandidateSelector()

    assert selector.flush() is None


def test_out_of_order_timestamp_raises_error() -> None:
    selector = BestCandidateSelector()

    selector.add(make_candidate(2.0, 20.0))

    with pytest.raises(ValueError):
        selector.add(make_candidate(1.0, 30.0))
