import unittest

from market_regime.inference import RegimeEngine
from market_regime.enums import Timeframe, RegimeLabel, TradePermission
from market_regime.schemas import Bar, InstrumentSeries
from .factory import make_series, trending_up_closes


class TestFailureModes(unittest.TestCase):
    def test_corrupted_ohlc_flagged_in_reason_codes_not_silently_ignored(self):
        engine = RegimeEngine()
        closes = trending_up_closes(n=300)
        series = make_series("NIFTY50", closes)
        bars = list(series.bars)
        bad = bars[-1]
        bars[-1] = Bar(bad.timestamp, bad.open, high=bad.low - 1, low=bad.high + 1,
                        close=bad.close, volume=bad.volume)  # high < low corruption
        corrupted = InstrumentSeries(symbol=series.symbol, timeframe=series.timeframe, bars=tuple(bars))
        state = engine.get_current_regime({Timeframe.D1: corrupted}, symbol_scope="MARKET:CORRUPT")
        self.assertIn("OHLC_HIGH_LT_LOW", state.reason_codes)

    def test_gap_crash_pushes_toward_panic_or_high_vol(self):
        engine = RegimeEngine()
        closes = trending_up_closes(n=295, daily_drift=0.001, noise=0.001)
        # sudden overnight gap down of 12%
        closes = closes + [closes[-1] * 0.88] * 5
        series = make_series("NIFTY50", closes)
        state = engine.get_current_regime({Timeframe.D1: series}, symbol_scope="MARKET:GAP")
        self.assertIn(state.market_regime, (RegimeLabel.PANIC, RegimeLabel.HIGH_VOLATILITY, RegimeLabel.TRENDING_DOWN))

    def test_zero_volume_does_not_crash_engine(self):
        engine = RegimeEngine()
        closes = trending_up_closes(n=300)
        series = make_series("NIFTY50", closes, vol_base=0)
        state = engine.get_current_regime({Timeframe.D1: series}, symbol_scope="MARKET:ZEROVOL")
        self.assertIsNotNone(state.market_regime)

    def test_never_returns_none_permission(self):
        engine = RegimeEngine()
        state = engine.get_current_regime({}, symbol_scope="MARKET:X")
        self.assertIsInstance(state.trade_permission, TradePermission)

    def test_unknown_state_never_allows_trading(self):
        engine = RegimeEngine()
        state = engine.get_current_regime({}, symbol_scope="MARKET:X")
        self.assertEqual(state.market_regime, RegimeLabel.UNKNOWN)
        self.assertNotEqual(state.trade_permission, TradePermission.ALLOW)
        self.assertEqual(state.recommended_position_multiplier, 0.0)


if __name__ == "__main__":
    unittest.main()
