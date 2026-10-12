"""
Correlation interface (spec §13).

The Portfolio Manager does not compute correlation itself (that would
duplicate a statistical model and risk look-ahead bias in backtests — see
DESIGN PART 8/17). It only *consumes* a correlation source supplied by the
caller, which must already be point-in-time correct.
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

from .enums import ValidationState


class CorrelationProvider(ABC):
    """Abstract interface. Backtests and live trading each supply their own
    point-in-time-correct implementation."""

    @abstractmethod
    def pairwise(self, symbol_a: str, symbol_b: str) -> Tuple[Optional[float], ValidationState]:
        """Return (correlation, state). State is UNKNOWN if not measured."""

    def candidate_to_portfolio(
        self, symbol: str, portfolio_symbols: list
    ) -> Tuple[Optional[float], ValidationState]:
        """Max pairwise correlation between `symbol` and any portfolio symbol.
        Default implementation built from `pairwise`; providers may override
        for efficiency (e.g. precomputed matrices)."""
        if not portfolio_symbols:
            return None, ValidationState.UNAVAILABLE

        max_corr = None
        any_unknown = False
        for other in portfolio_symbols:
            if other == symbol:
                continue
            corr, state = self.pairwise(symbol, other)
            if state == ValidationState.UNKNOWN or corr is None:
                any_unknown = True
                continue
            if max_corr is None or corr > max_corr:
                max_corr = corr

        if max_corr is not None:
            # We found at least one known correlation; report it even if
            # other pairs were unknown (conservative: caller sees the
            # highest KNOWN correlation, and can separately note the gap).
            return max_corr, ValidationState.VALID
        if any_unknown:
            return None, ValidationState.UNKNOWN
        return None, ValidationState.UNAVAILABLE


class StaticCorrelationProvider(CorrelationProvider):
    """Simple provider backed by an explicit symbol-pair matrix (dict).
    Suitable for Phase 3A scale (10-50 candidates) and for backtests where
    a rolling-correlation matrix was already computed upstream at time T."""

    def __init__(self, matrix: Optional[Dict[Tuple[str, str], float]] = None):
        self._matrix: Dict[Tuple[str, str], float] = dict(matrix or {})

    def set(self, symbol_a: str, symbol_b: str, correlation: float) -> None:
        self._matrix[(symbol_a, symbol_b)] = correlation
        self._matrix[(symbol_b, symbol_a)] = correlation

    def pairwise(self, symbol_a: str, symbol_b: str) -> Tuple[Optional[float], ValidationState]:
        if symbol_a == symbol_b:
            return 1.0, ValidationState.VALID
        key = (symbol_a, symbol_b)
        if key in self._matrix:
            return self._matrix[key], ValidationState.VALID
        return None, ValidationState.UNKNOWN
