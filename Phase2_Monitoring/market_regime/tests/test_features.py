import unittest

from market_regime._math_utils import sma, average_true_range, adx, percentile_rank, linreg_slope, pct_change
from market_regime.trend import compute_trend
from market_regime.volatility import compute_volatility
from market_regime.enums import TrendState, VolatilityState
from market_regime.config import DEFAULT_CONFIG
from .factory import trending_up_closes, sideways_closes, low_vol_closes, make_series


class TestMathUtils(unittest.TestCase):
    def test_sma_basic(self):
        self.assertAlmostEqual(sma([1, 2, 3, 4, 5], 3), 4.0)

    def test_sma_insufficient_history_returns_none(self):
        self.assertIsNone(sma([1, 2], 3))

    def test_no_lookahead_sma(self):
        values = list(range(1, 21))
        # sma computed "as of" index 5 must not see index 6+
        v_at_5 = sma(values, 3, end_index=5)
        truncated = sma(values[:6], 3)
        self.assertEqual(v_at_5, truncated)

    def test_pct_change(self):
        self.assertAlmostEqual(pct_change([100, 110], 1), 0.10)

    def test_percentile_rank(self):
        hist = [1, 2, 3, 4, 5]
        self.assertAlmostEqual(percentile_rank(hist, 3), 0.6)

    def test_linreg_slope_sign(self):
        up = [1, 2, 3, 4, 5]
        down = [5, 4, 3, 2, 1]
        self.assertGreater(linreg_slope(up), 0)
        self.assertLess(linreg_slope(down), 0)


class TestTrendFeature(unittest.TestCase):
    def test_uptrend_detected_strong_up_or_weak_up(self):
        closes = trending_up_closes(n=200, daily_drift=0.004, noise=0.001)
        highs = [c * 1.005 for c in closes]
        lows = [c * 0.995 for c in closes]
        state, score, reasons = compute_trend(closes, highs, lows, DEFAULT_CONFIG.trend)
        self.assertIn(state, (TrendState.STRONG_UP, TrendState.WEAK_UP))
        self.assertGreater(score, 0)

    def test_sideways_detected_flat(self):
        closes = sideways_closes(n=200, noise=0.002)
        highs = [c * 1.005 for c in closes]
        lows = [c * 0.995 for c in closes]
        state, score, reasons = compute_trend(closes, highs, lows, DEFAULT_CONFIG.trend)
        self.assertEqual(state, TrendState.FLAT)

    def test_insufficient_history_returns_unknown(self):
        closes = [100.0] * 5
        state, score, reasons = compute_trend(closes, closes, closes, DEFAULT_CONFIG.trend)
        self.assertEqual(state, TrendState.UNKNOWN)
        self.assertIn("INSUFFICIENT_HISTORY_TREND", reasons)


class TestVolatilityFeature(unittest.TestCase):
    def test_low_vol_series_scores_low_or_normal(self):
        closes = low_vol_closes(n=300, noise=0.0005)
        highs = [c * 1.0005 for c in closes]
        lows = [c * 0.9995 for c in closes]
        state, pct, reasons = compute_volatility(closes, highs, lows, DEFAULT_CONFIG.volatility)
        self.assertIn(state, (VolatilityState.LOW, VolatilityState.NORMAL))

    def test_insufficient_history_returns_unknown(self):
        closes = [100.0] * 10
        state, pct, reasons = compute_volatility(closes, closes, closes, DEFAULT_CONFIG.volatility)
        self.assertEqual(state, VolatilityState.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
