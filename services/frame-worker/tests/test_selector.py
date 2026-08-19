import numpy as np
import pytest

from frame_worker.processing.selector import (
    BestCandidateSelector,
    FrameCandidate,
    ShortlistCandidateSelector,
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


def test_shortlist_keeps_top_k_candidates_per_window() -> None:
    selector = ShortlistCandidateSelector(window_seconds=1.0, shortlist_size=2)

    selector.add(make_candidate(0.1, 10.0))
    selector.add(make_candidate(0.2, 50.0))
    selector.add(make_candidate(0.3, 30.0))
    completed = selector.add(make_candidate(1.1, 20.0))

    assert completed is not None
    assert [item.quality.quality for item in completed] == [50.0, 30.0]
    assert [item.quality.quality for item in selector.flush()] == [20.0]


def adaptive_scores(scores: list[float]) -> list[float]:
    selector = ShortlistCandidateSelector(
        shortlist_size=3,
        adaptive_enabled=True,
        adaptive_threshold=0.04,
    )
    for index, score in enumerate(scores):
        selector.add(make_candidate(index / 10, score, index + 1))
    return [candidate.quality.quality for candidate in selector.flush()]


def test_adaptive_gap_above_threshold_uses_top_two() -> None:
    assert adaptive_scores([200.0, 100.0, 95.0]) == [200.0, 100.0]


def test_adaptive_gap_equal_to_threshold_keeps_top_three() -> None:
    assert adaptive_scores([200.0, 100.0, 96.0]) == [200.0, 100.0, 96.0]


def test_adaptive_gap_below_threshold_keeps_top_three() -> None:
    assert adaptive_scores([200.0, 100.0, 98.0]) == [200.0, 100.0, 98.0]


def test_adaptive_equal_second_and_third_scores_keep_top_three() -> None:
    assert adaptive_scores([200.0, 100.0, 100.0]) == [200.0, 100.0, 100.0]


def test_adaptive_zero_scores_are_safe() -> None:
    assert adaptive_scores([1.0, 0.0, 0.0]) == [1.0, 0.0, 0.0]


def test_adaptive_negative_scores_are_safe() -> None:
    assert adaptive_scores([1.0, -1.0, -2.0]) == [1.0, -1.0]


def test_adaptive_window_with_two_candidates_is_safe() -> None:
    assert adaptive_scores([20.0, 10.0]) == [20.0, 10.0]


def test_disabled_adaptive_shortlist_keeps_configured_size() -> None:
    selector = ShortlistCandidateSelector(
        shortlist_size=3,
        adaptive_enabled=False,
        adaptive_threshold=0.04,
    )
    selector.add(make_candidate(0.1, 200.0))
    selector.add(make_candidate(0.2, 100.0))
    selector.add(make_candidate(0.3, 1.0))

    assert len(selector.flush()) == 3
