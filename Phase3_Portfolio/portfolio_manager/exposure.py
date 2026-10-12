"""
Sector and Strategy exposure tracking (spec §14-15).

Both dimensions share identical mechanics: what fraction of the current
(open positions + already-selected-this-round) portfolio shares the
candidate's key. Implemented once, parametrized by a key function, to avoid
duplicated logic (spec explicitly separates these as sections but the
underlying math is the same).
"""

from collections import Counter
from typing import Callable, Iterable, List, Tuple


def _exposure_fraction(keys: List[str], candidate_key: str) -> float:
    if not keys:
        return 0.0
    counts = Counter(keys)
    return counts.get(candidate_key, 0) / len(keys)


class ExposureTracker:
    """Tracks concentration for one dimension (sector or strategy)."""

    def __init__(self, name: str, threshold: float, overlap_penalty: float):
        self.name = name
        self.threshold = threshold
        self.overlap_penalty = overlap_penalty

    def current_exposure(self, existing_keys: List[str]) -> dict:
        if not existing_keys:
            return {}
        counts = Counter(existing_keys)
        total = len(existing_keys)
        return {k: v / total for k, v in counts.items()}

    def concentration_flag(self, existing_keys: List[str], candidate_key: str) -> bool:
        """Would adding this candidate push exposure at/above threshold?"""
        projected = existing_keys + [candidate_key]
        frac = _exposure_fraction(projected, candidate_key)
        return frac >= self.threshold

    def penalty(self, existing_keys: List[str], candidate_key: str) -> float:
        """Normalized penalty in [0, overlap_penalty], scaled by how far
        projected exposure exceeds the threshold (0 if under threshold)."""
        if not existing_keys:
            return 0.0
        projected = existing_keys + [candidate_key]
        frac = _exposure_fraction(projected, candidate_key)
        if frac < self.threshold:
            return 0.0
        # Scale linearly from threshold (→ 0 penalty) to 1.0 (→ full penalty)
        span = max(1.0 - self.threshold, 1e-9)
        scaled = (frac - self.threshold) / span
        return min(scaled, 1.0) * self.overlap_penalty
