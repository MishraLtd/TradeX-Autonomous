import unittest

from market_regime.transitions import RegimeHistory
from market_regime.config import StabilityConfig
from market_regime.enums import RegimeLabel


class TestHysteresis(unittest.TestCase):
    def test_single_noisy_flip_does_not_change_confirmed_regime(self):
        cfg = StabilityConfig(min_regime_duration_bars=3, confirmation_bars=2)
        hist = RegimeHistory(cfg=cfg)
        votes = [
            RegimeLabel.TRENDING_UP, RegimeLabel.TRENDING_UP, RegimeLabel.TRENDING_UP,
            RegimeLabel.SIDEWAYS,  # single noisy flip
            RegimeLabel.TRENDING_UP, RegimeLabel.TRENDING_UP,
        ]
        confirmed = None
        for i, v in enumerate(votes):
            confirmed = hist.update(float(i), v)
        self.assertEqual(confirmed, RegimeLabel.TRENDING_UP)

    def test_sustained_flip_eventually_confirms(self):
        cfg = StabilityConfig(min_regime_duration_bars=2, confirmation_bars=2)
        hist = RegimeHistory(cfg=cfg)
        votes = (
            [RegimeLabel.TRENDING_UP] * 3
            + [RegimeLabel.SIDEWAYS] * 5  # sustained regime change
        )
        confirmed = None
        for i, v in enumerate(votes):
            confirmed = hist.update(float(i), v)
        self.assertEqual(confirmed, RegimeLabel.SIDEWAYS)

    def test_panic_overrides_min_duration_lock(self):
        cfg = StabilityConfig(min_regime_duration_bars=10, confirmation_bars=5)
        hist = RegimeHistory(cfg=cfg)
        for i in range(3):
            hist.update(float(i), RegimeLabel.TRENDING_UP)
        # regime is locked in (duration < min_regime_duration_bars) but PANIC
        # must still break through immediately (capital preservation wins)
        confirmed = hist.update(3.0, RegimeLabel.PANIC)
        self.assertEqual(confirmed, RegimeLabel.PANIC)

    def test_duration_counter_resets_on_confirmed_change(self):
        cfg = StabilityConfig(min_regime_duration_bars=1, confirmation_bars=1)
        hist = RegimeHistory(cfg=cfg)
        for i in range(5):
            hist.update(float(i), RegimeLabel.TRENDING_UP)
        self.assertEqual(hist.duration_bars, 5)
        hist.update(5.0, RegimeLabel.SIDEWAYS)
        self.assertEqual(hist.duration_bars, 1)

    def test_stability_score_high_when_stable(self):
        cfg = StabilityConfig(min_regime_duration_bars=1, confirmation_bars=1)
        hist = RegimeHistory(cfg=cfg)
        for i in range(20):
            hist.update(float(i), RegimeLabel.TRENDING_UP)
        self.assertGreaterEqual(hist.stability_score(), 0.99)

    def test_stability_score_lower_when_flapping(self):
        cfg = StabilityConfig(min_regime_duration_bars=1, confirmation_bars=1)
        hist = RegimeHistory(cfg=cfg)
        labels = [RegimeLabel.TRENDING_UP, RegimeLabel.SIDEWAYS] * 10
        for i, l in enumerate(labels):
            hist.update(float(i), l)
        self.assertLess(hist.stability_score(), 0.7)

    def test_transition_signal_toward_panic(self):
        cfg = StabilityConfig(min_regime_duration_bars=1, confirmation_bars=1)
        hist = RegimeHistory(cfg=cfg)
        for i in range(5):
            hist.update(float(i), RegimeLabel.TRENDING_DOWN)
        prob, direction = hist.transition_signal(RegimeLabel.PANIC)
        self.assertGreater(prob, 0.0)


if __name__ == "__main__":
    unittest.main()
