import unittest

from market_regime.inference import RegimeEngine
from market_regime.enums import RegimeLabel, Timeframe, TradePermission
from .factory import (
    make_series, trending_up_closes, trending_down_closes, sideways_closes,
    low_vol_closes, panic_closes,
)


def build_market_series(closes, timeframe=Timeframe.D1):
    return {timeframe: make_series("NIFTY50", closes, timeframe=timeframe)}


class TestRegimeClassifierScenarios(unittest.TestCase):
    def test_trending_up_market(self):
        engine = RegimeEngine()
        closes = trending_up_closes(n=300, daily_drift=0.004, noise=0.001)
        series = build_market_series(closes)
        state = engine.get_current_regime(series, symbol_scope="MARKET:TEST_UP")
        self.assertEqual(state.market_regime, RegimeLabel.TRENDING_UP)
        self.assertIn(state.trade_permission, (TradePermission.ALLOW, TradePermission.REDUCE))
        self.assertGreater(state.recommended_position_multiplier, 0.0)

    def test_trending_down_market(self):
        engine = RegimeEngine()
        closes = trending_down_closes(n=300, daily_drift=-0.004, noise=0.001)
        series = build_market_series(closes)
        state = engine.get_current_regime(series, symbol_scope="MARKET:TEST_DOWN")
        self.assertEqual(state.market_regime, RegimeLabel.TRENDING_DOWN)

    def test_sideways_market(self):
        engine = RegimeEngine()
        closes = sideways_closes(n=300, noise=0.002)
        series = build_market_series(closes)
        state = engine.get_current_regime(series, symbol_scope="MARKET:TEST_SIDEWAYS")
        self.assertEqual(state.market_regime, RegimeLabel.SIDEWAYS)

    def test_low_volatility_market(self):
        engine = RegimeEngine()
        closes = low_vol_closes(n=300, noise=0.0004)
        series = build_market_series(closes)
        state = engine.get_current_regime(series, symbol_scope="MARKET:TEST_LOWVOL")
        self.assertIn(state.dimensions.volatility_state.value, ("LOW", "NORMAL"))

    def test_panic_market_blocks_new_trades(self):
        engine = RegimeEngine()
        closes = panic_closes(n=300)
        series = build_market_series(closes)
        state = engine.get_current_regime(series, symbol_scope="MARKET:TEST_PANIC")
        self.assertEqual(state.market_regime, RegimeLabel.PANIC)
        self.assertEqual(state.trade_permission, TradePermission.BLOCK)
        self.assertEqual(state.recommended_position_multiplier, 0.0)

    def test_no_series_returns_unknown_and_blocks(self):
        engine = RegimeEngine()
        state = engine.get_current_regime({}, symbol_scope="MARKET:EMPTY")
        self.assertEqual(state.market_regime, RegimeLabel.UNKNOWN)
        self.assertEqual(state.trade_permission, TradePermission.BLOCK)

    def test_insufficient_history_returns_unknown_and_blocks(self):
        engine = RegimeEngine()
        closes = trending_up_closes(n=10)  # far below min_bars_required for daily
        series = build_market_series(closes)
        state = engine.get_current_regime(series, symbol_scope="MARKET:SHORT_HIST")
        self.assertEqual(state.market_regime, RegimeLabel.UNKNOWN)
        self.assertEqual(state.trade_permission, TradePermission.BLOCK)
        self.assertFalse(state.data_freshness_ok)

    def test_stale_data_blocks_even_with_good_trend(self):
        engine = RegimeEngine()
        closes = trending_up_closes(n=300, daily_drift=0.004, noise=0.001)
        series = build_market_series(closes)
        far_future = series[Timeframe.D1].bars[-1].timestamp + 3600 * 24 * 365  # 1yr later
        state = engine.get_current_regime(series, symbol_scope="MARKET:STALE", now=far_future)
        self.assertEqual(state.market_regime, RegimeLabel.UNKNOWN)
        self.assertEqual(state.trade_permission, TradePermission.BLOCK)

    def test_multi_timeframe_conflict_reduces_alignment_score(self):
        engine = RegimeEngine()
        up_closes = trending_up_closes(n=300, daily_drift=0.004, noise=0.001)
        down_closes = trending_down_closes(n=300, daily_drift=-0.004, noise=0.001)
        series = {
            Timeframe.M15: make_series("NIFTY50", down_closes, timeframe=Timeframe.M15, step=900),
            Timeframe.D1: make_series("NIFTY50", up_closes, timeframe=Timeframe.D1),
        }
        state = engine.get_current_regime(series, symbol_scope="MARKET:CONFLICT")
        self.assertLess(state.regime_alignment_score, 1.0)

    def test_market_sector_stock_alignment(self):
        engine = RegimeEngine()
        up_closes = trending_up_closes(n=300, daily_drift=0.004, noise=0.001)
        flat_closes = sideways_closes(n=300, noise=0.002)
        market = build_market_series(up_closes)
        sector = build_market_series(up_closes)
        stock = build_market_series(flat_closes)
        state = engine.get_current_regime(
            market, symbol_scope="MARKET:ALIGN",
            sector_series=sector, sector_scope="SECTOR:BANK",
            stock_series=stock, stock_scope="STOCK:XYZ",
        )
        self.assertIsNotNone(state.cross_level_alignment_score)
        self.assertLess(state.cross_level_alignment_score, 1.0)

    def test_high_confidence_vs_low_confidence_scenarios(self):
        # This exercises rule_based.classify_regime/heuristic_confidence
        # directly with a clean single-dimension read vs a genuinely
        # conflicting one (trend says UP, breadth+momentum say DOWN,
        # volatility is EXTREME) - the kind of situation §7/§29 says must
        # NOT be reported with the same confidence as a clean read.
        from market_regime.rule_based import classify_regime, heuristic_confidence
        from market_regime.schemas import DimensionScores
        from market_regime.enums import (
            TrendState, VolatilityState, BreadthState, MomentumState, StressLevel,
        )

        clean = DimensionScores(
            trend_state=TrendState.STRONG_UP, trend_score=0.9,
            volatility_state=VolatilityState.NORMAL, volatility_percentile=0.5,
            breadth_state=BreadthState.STRONG_POSITIVE, breadth_ratio=0.8,
            momentum_state=MomentumState.POSITIVE, momentum_score=0.05,
            stress_level=StressLevel.NORMAL, stress_score=0,
        )
        conflicted = DimensionScores(
            trend_state=TrendState.WEAK_UP, trend_score=0.2,
            volatility_state=VolatilityState.EXTREME, volatility_percentile=0.99,
            breadth_state=BreadthState.STRONG_NEGATIVE, breadth_ratio=0.2,
            momentum_state=MomentumState.NEGATIVE, momentum_score=-0.04,
            stress_level=StressLevel.ELEVATED, stress_score=2,
        )

        conf_clean = heuristic_confidence(classify_regime(clean))
        conf_conflicted = heuristic_confidence(classify_regime(conflicted))
        self.assertGreater(conf_clean, conf_conflicted)


if __name__ == "__main__":
    unittest.main()
