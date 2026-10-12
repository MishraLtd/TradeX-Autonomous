"""
Regime history tracker: turns a stream of raw (per-bar) classifier votes
into a STABLE confirmed regime timeline, and derives duration/transition
signals from that timeline.

This is the component directly responsible for solving §18 ("regime
flapping"). Two independent mechanisms are combined:

1. Confirmation window: a new label must win N consecutive raw votes
   before it is accepted as the confirmed regime (cfg.confirmation_bars).
2. Minimum duration: once confirmed, a regime cannot flip again for at
   least cfg.min_regime_duration_bars, UNLESS the new candidate is PANIC
   (stress overrides duration lock - §16/§45 capital preservation wins
   over label stability when the market is actually panicking).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional

from .enums import RegimeLabel, TransitionDirection
from .config import StabilityConfig


_DIRECTION_MAP = {
    RegimeLabel.TRENDING_UP: TransitionDirection.TOWARD_TREND_UP,
    RegimeLabel.TRENDING_DOWN: TransitionDirection.TOWARD_TREND_DOWN,
    RegimeLabel.SIDEWAYS: TransitionDirection.TOWARD_SIDEWAYS,
    RegimeLabel.HIGH_VOLATILITY: TransitionDirection.TOWARD_HIGH_VOL,
    RegimeLabel.LOW_VOLATILITY: TransitionDirection.TOWARD_LOW_VOL,
    RegimeLabel.PANIC: TransitionDirection.TOWARD_PANIC,
}


@dataclass
class RegimeHistoryEntry:
    timestamp: float
    raw_label: RegimeLabel
    confirmed_label: RegimeLabel


@dataclass
class RegimeHistory:
    """Stateful tracker. One instance per (symbol_scope, timeframe) pair -
    the caller (inference.py) owns persistence of this object between
    calls (in-memory cache keyed by scope; §19 recalculation frequency)."""
    cfg: StabilityConfig = field(default_factory=StabilityConfig)
    entries: List[RegimeHistoryEntry] = field(default_factory=list)

    @property
    def confirmed_label(self) -> Optional[RegimeLabel]:
        return self.entries[-1].confirmed_label if self.entries else None

    @property
    def duration_bars(self) -> int:
        if not self.entries:
            return 0
        current = self.entries[-1].confirmed_label
        count = 0
        for e in reversed(self.entries):
            if e.confirmed_label != current:
                break
            count += 1
        return count

    def _recent_raw_run_length(self, label: RegimeLabel) -> int:
        count = 0
        for e in reversed(self.entries):
            if e.raw_label != label:
                break
            count += 1
        return count

    def update(self, timestamp: float, raw_label: RegimeLabel) -> RegimeLabel:
        """Feed one new raw vote; returns the (possibly unchanged) confirmed
        label for this bar. This is the ONLY mutating entry point."""
        prev_confirmed = self.confirmed_label

        if prev_confirmed is None:
            confirmed = raw_label
        elif raw_label == prev_confirmed:
            confirmed = prev_confirmed
        else:
            # candidate differs from current confirmed regime
            run_length = self._recent_raw_run_length(raw_label) + 1  # including this vote
            panic_override = raw_label == RegimeLabel.PANIC
            duration_ok = self.duration_bars >= self.cfg.min_regime_duration_bars
            confirmation_ok = run_length >= self.cfg.confirmation_bars

            if panic_override or (duration_ok and confirmation_ok):
                confirmed = raw_label
            else:
                confirmed = prev_confirmed

        self.entries.append(RegimeHistoryEntry(timestamp, raw_label, confirmed))
        return confirmed

    def stability_score(self, window: int = 20) -> float:
        """Fraction of the last `window` bars whose CONFIRMED label matches
        the current one - a simple, interpretable stability measure (§33
        regime_stability_score). 1.0 = perfectly stable, low = flapping."""
        recent = self.entries[-window:]
        if not recent:
            return 0.0
        current = recent[-1].confirmed_label
        matches = sum(1 for e in recent if e.confirmed_label == current)
        return matches / len(recent)

    def transition_signal(self, raw_label: RegimeLabel):
        """Returns (transition_probability, transition_direction) using the
        raw (unconfirmed) vote as an early-warning signal against the
        confirmed regime - this is intentionally simple (fraction of recent
        raw votes disagreeing with the confirmed label) rather than a
        fitted Markov transition matrix, which needs labeled historical
        data to estimate reliably (§6 flags this as worth testing further)."""
        confirmed = self.confirmed_label
        if confirmed is None:
            return 0.0, TransitionDirection.NONE
        if raw_label == confirmed:
            recent = self.entries[-10:]
            if not recent:
                return 0.0, TransitionDirection.NONE
            disagree = sum(1 for e in recent if e.raw_label != confirmed)
            return disagree / len(recent), TransitionDirection.NONE

        recent = self.entries[-10:]
        disagree_with_confirmed = sum(
            1 for e in recent if e.raw_label != confirmed
        ) + 1  # include current vote
        prob = min(1.0, disagree_with_confirmed / (len(recent) + 1))
        direction = _DIRECTION_MAP.get(raw_label, TransitionDirection.NONE)
        if confirmed == RegimeLabel.PANIC and raw_label != RegimeLabel.PANIC:
            direction = TransitionDirection.TOWARD_RECOVERY
        return prob, direction
