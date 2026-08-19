from dataclasses import dataclass

import numpy as np

from frame_worker.quality.scorer import QualityResult
from frame_worker.sampling.sampler import calculate_window_index


@dataclass(frozen=True)
class FrameCandidate:
    frame: np.ndarray
    frame_number: int
    timestamp: float
    quality: QualityResult


class BestCandidateSelector:
    """Select the highest-quality candidate from each time window."""

    def __init__(self, window_seconds: float = 1.0) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than zero")

        self.window_seconds = window_seconds
        self._current_window: int | None = None
        self._best_candidate: FrameCandidate | None = None
        self._last_timestamp: float | None = None

    def add(
        self,
        candidate: FrameCandidate,
    ) -> FrameCandidate | None:
        """Add a candidate and return the completed window's best frame."""

        if (
            self._last_timestamp is not None
            and candidate.timestamp < self._last_timestamp
        ):
            raise ValueError("candidate timestamps must be in chronological order")

        self._last_timestamp = candidate.timestamp

        window = calculate_window_index(
            candidate.timestamp,
            self.window_seconds,
        )

        if self._current_window is None:
            self._current_window = window
            self._best_candidate = candidate
            return None

        if window == self._current_window:
            if (
                self._best_candidate is None
                or candidate.quality.quality > self._best_candidate.quality.quality
            ):
                self._best_candidate = candidate

            return None

        completed_candidate = self._best_candidate

        self._current_window = window
        self._best_candidate = candidate

        return completed_candidate

    def flush(self) -> FrameCandidate | None:
        """Return the best candidate remaining in the final window."""

        candidate = self._best_candidate

        self._best_candidate = None
        self._current_window = None
        self._last_timestamp = None

        return candidate


class ShortlistCandidateSelector:
    """Keep the top-K fast-quality candidates from each time window."""

    def __init__(self, window_seconds: float = 1.0, shortlist_size: int = 3) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than zero")
        if shortlist_size <= 0:
            raise ValueError("shortlist_size must be greater than zero")

        self.window_seconds = window_seconds
        self.shortlist_size = shortlist_size
        self._current_window: int | None = None
        self._candidates: list[FrameCandidate] = []
        self._last_timestamp: float | None = None

    def add(self, candidate: FrameCandidate) -> list[FrameCandidate] | None:
        if (
            self._last_timestamp is not None
            and candidate.timestamp < self._last_timestamp
        ):
            raise ValueError("candidate timestamps must be in chronological order")
        self._last_timestamp = candidate.timestamp
        window = calculate_window_index(candidate.timestamp, self.window_seconds)

        if self._current_window is None:
            self._current_window = window
        elif window != self._current_window:
            completed = self._ranked_candidates()
            self._current_window = window
            self._candidates = [candidate]
            return completed

        self._candidates.append(candidate)
        return None

    def flush(self) -> list[FrameCandidate]:
        candidates = self._ranked_candidates()
        self._current_window = None
        self._candidates = []
        self._last_timestamp = None
        return candidates

    def _ranked_candidates(self) -> list[FrameCandidate]:
        return sorted(
            self._candidates,
            key=lambda candidate: candidate.quality.quality,
            reverse=True,
        )[: self.shortlist_size]
