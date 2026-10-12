"""
RegimeEngine: the single integration point the rest of TradeX talks to.

    engine = RegimeEngine()
    state = engine.get_current_regime(market_series, symbol_scope="MARKET:NIFTY50")
    mult = engine.get_position_multiplier(state)
    permission = engine.should_allow_new_trade(state, strategy="momentum")
    compat = engine.evaluate_regime_compatibility("momentum", state)

Every public method is READ-ONLY with respect to trading - this module
never touches an order, a broker connection, or the Risk Engine's limits
(§54). It only returns a RegimeState (or a derived scalar) for callers to
combine with their own authority.
"""

from __future__ import annotations
import time
from typing import Dict, Optional, List

from .config import RegimeConfig, DEFAULT_CONFIG, MODEL_VERSION, FEATURE_VERSION
from .enums import (
    RegimeLabel, Timeframe, TradePermission, TransitionDirection, StressLevel,
)
from .schemas import (
    InstrumentSeries, BreadthSnapshot, DimensionScores, RegimeProbabilities,
    RegimeState, StrategyCompatibility,
)
from .trend import compute_trend
from .volatility import compute_volatility
from .breadth import compute_breadth
from .momentum import compute_momentum
from .stress import compute_stress
from .rule_based import classify_regime, heuristic_confidence
from .transitions import RegimeHistory
from .strategy_compatibility import compute_strategy_compatibility
from .position_adjustment import compute_position_multiplier_and_gating
from .calibration import DEFAULT_CALIBRATOR
from . import validator
from .exceptions import InsufficientDataError, StaleDataError, ModelUnavailableError


# Which provided timeframe plays which role in the 3-tier framework (§5).
# The finest available timeframe present in the input becomes "short", the
# coarsest becomes "long"; anything in between (or duplicated) is "medium".
_TIMEFRAME_ORDER = [Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.D1, Timeframe.W1]


def _closes_highs_lows_vols(series: InstrumentSeries):
    closes = [b.close for b in series.bars]
    highs = [b.high for b in series.bars]
    lows = [b.low for b in series.bars]
    vols = [b.volume for b in series.bars]
    return closes, highs, lows, vols


class RegimeEngine:
    def __init__(self, config: RegimeConfig = None):
        self.config = config or DEFAULT_CONFIG
        self.calibrator = DEFAULT_CALIBRATOR
        self._histories: Dict[str, RegimeHistory] = {}  # key: f"{scope}:{timeframe}"

    # ------------------------------------------------------------------
    # single-timeframe classification (building block)
    # ------------------------------------------------------------------
    def _classify_single_timeframe(
        self,
        series: InstrumentSeries,
        breadth: Optional[BreadthSnapshot] = None,
        now: Optional[float] = None,
    ):
        """Returns (RegimeLabel raw vote, DimensionScores, RegimeProbabilities,
        heuristic_confidence, ohlc_issues). Raises InsufficientDataError /
        StaleDataError if the series fails validator checks - callers MUST
        catch these and fail safe (UNKNOWN/BLOCK), never swallow silently."""
        validator.check_sufficiency(series, self.config.freshness)
        validator.check_freshness(series, self.config.freshness, now=now)
        ohlc_issues = validator.sanity_check_ohlc(series)

        closes, highs, lows, vols = _closes_highs_lows_vols(series)

        trend_state, trend_score, trend_reasons = compute_trend(closes, highs, lows, self.config.trend)
        vol_state, vol_pct, vol_reasons = compute_volatility(closes, highs, lows, self.config.volatility)
        breadth_state, breadth_ratio, breadth_reasons = compute_breadth(breadth, self.config.breadth)
        mom_state, mom_score, mom_reasons = compute_momentum(closes, self.config.momentum)
        stress_level, stress_score, stress_reasons = compute_stress(
            closes, vols, vol_pct, breadth_ratio, cfg=self.config.stress
        )

        reasons = tuple(trend_reasons + vol_reasons + breadth_reasons + mom_reasons + stress_reasons + ohlc_issues)

        dims = DimensionScores(
            trend_state=trend_state, trend_score=trend_score,
            volatility_state=vol_state, volatility_percentile=vol_pct,
            breadth_state=breadth_state, breadth_ratio=breadth_ratio,
            momentum_state=mom_state, momentum_score=mom_score,
            stress_level=stress_level, stress_score=stress_score,
            reason_codes=reasons,
        )

        probs = classify_regime(dims)
        raw_confidence = heuristic_confidence(probs)
        confidence = self.calibrator.calibrate(raw_confidence)
        raw_label_str, _ = probs.top()
        raw_label = RegimeLabel(raw_label_str) if raw_label_str else RegimeLabel.UNKNOWN

        return raw_label, dims, probs, confidence, ohlc_issues

    def _get_history(self, scope: str, timeframe: Timeframe) -> RegimeHistory:
        key = f"{scope}:{timeframe.value}"
        if key not in self._histories:
            self._histories[key] = RegimeHistory(cfg=self.config.stability)
        return self._histories[key]

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def get_current_regime(
        self,
        market_series: Dict[Timeframe, InstrumentSeries],
        symbol_scope: str = "MARKET:NIFTY50",
        breadth: Optional[BreadthSnapshot] = None,
        sector_series: Optional[Dict[Timeframe, InstrumentSeries]] = None,
        sector_scope: Optional[str] = None,
        stock_series: Optional[Dict[Timeframe, InstrumentSeries]] = None,
        stock_scope: Optional[str] = None,
        now: Optional[float] = None,
    ) -> RegimeState:
        now = now if now is not None else time.time()
        available_tfs = [tf for tf in _TIMEFRAME_ORDER if tf in market_series]

        if not available_tfs:
            return self._unknown_state(symbol_scope, now, reasons=("NO_TIMEFRAMES_PROVIDED",))

        short_tf = available_tfs[0]
        long_tf = available_tfs[-1]
        medium_tf = available_tfs[len(available_tfs) // 2]

        per_tf_result = {}
        fail_reasons = []
        for tf in available_tfs:
            try:
                raw_label, dims, probs, conf, issues = self._classify_single_timeframe(
                    market_series[tf], breadth=breadth if tf == short_tf else None, now=now
                )
                history = self._get_history(symbol_scope, tf)
                confirmed = history.update(now, raw_label)
                per_tf_result[tf] = {
                    "raw": raw_label, "confirmed": confirmed, "dims": dims,
                    "probs": probs, "confidence": conf, "history": history,
                }
            except (InsufficientDataError, StaleDataError) as e:
                fail_reasons.append(f"{tf.value}_UNAVAILABLE:{e}")

        # §44: if the SHORT (decision) timeframe is unusable, fail the whole read
        if short_tf not in per_tf_result:
            return self._unknown_state(symbol_scope, now, reasons=tuple(fail_reasons))

        primary = per_tf_result[short_tf]
        medium_regime = per_tf_result.get(medium_tf, primary)["confirmed"]
        long_regime = per_tf_result.get(long_tf, primary)["confirmed"]
        short_regime = primary["confirmed"]

        labels_seen = [short_regime, medium_regime, long_regime]
        agreement = sum(1 for l in labels_seen if l == short_regime) / len(labels_seen)

        history = primary["history"]
        stability = history.stability_score()
        duration = history.duration_bars
        transition_prob, transition_dir = history.transition_signal(primary["raw"])

        previous_regime = None
        if len(history.entries) >= 2:
            previous_regime = history.entries[-2].confirmed_label

        data_ok = len(fail_reasons) == 0

        strategy_compat = compute_strategy_compatibility(
            regime=short_regime,
            volatility_state=primary["dims"].volatility_state,
            regime_confidence=primary["confidence"],
            regime_stability=stability,
        )
        recommended_strategy, _ = strategy_compat.best()

        position_mult, permission = compute_position_multiplier_and_gating(
            regime=short_regime,
            confidence=primary["confidence"],
            stability=stability,
            stress_level=primary["dims"].stress_level,
            data_freshness_ok=data_ok,
            confidence_cfg=self.config.confidence,
        )

        # --- cross-level (market/sector/stock) alignment, §4 ---
        sector_regime = None
        stock_regime = None
        cross_alignment = None
        if sector_series:
            try:
                s_raw, s_dims, s_probs, s_conf, _ = self._classify_single_timeframe(
                    sector_series[short_tf], now=now
                )
                sector_history = self._get_history(sector_scope or f"{symbol_scope}:SECTOR", short_tf)
                sector_regime = sector_history.update(now, s_raw)
            except (InsufficientDataError, StaleDataError, KeyError):
                sector_regime = None
        if stock_series:
            try:
                k_raw, k_dims, k_probs, k_conf, _ = self._classify_single_timeframe(
                    stock_series[short_tf], now=now
                )
                stock_history = self._get_history(stock_scope or f"{symbol_scope}:STOCK", short_tf)
                stock_regime = stock_history.update(now, k_raw)
            except (InsufficientDataError, StaleDataError, KeyError):
                stock_regime = None

        levels = [l for l in (short_regime, sector_regime, stock_regime) if l is not None]
        if len(levels) > 1:
            cross_alignment = sum(1 for l in levels if l == short_regime) / len(levels)
            # PANIC at market level must not be diluted by a calm stock read (§4)
            if short_regime == RegimeLabel.PANIC:
                permission = TradePermission.BLOCK if permission != TradePermission.BLOCK else permission

        all_reasons = tuple(primary["dims"].reason_codes) + tuple(fail_reasons)

        return RegimeState(
            timestamp=now,
            symbol_scope=symbol_scope,
            market_regime=short_regime,
            market_regime_confidence=primary["confidence"],
            market_regime_probabilities=primary["probs"],
            short_term_regime=short_regime,
            medium_term_regime=medium_regime,
            long_term_regime=long_regime,
            regime_alignment_score=agreement,
            regime_stability_score=stability,
            regime_duration_bars=duration,
            previous_regime=previous_regime,
            transition_probability=transition_prob,
            transition_direction=transition_dir,
            dimensions=primary["dims"],
            sector_regime=sector_regime,
            stock_regime=stock_regime,
            cross_level_alignment_score=cross_alignment,
            strategy_compatibility=strategy_compat,
            recommended_strategy=recommended_strategy,
            recommended_position_multiplier=position_mult,
            trade_permission=permission,
            data_freshness_ok=data_ok,
            model_version=MODEL_VERSION,
            feature_version=FEATURE_VERSION,
            config_version=self.config.config_version,
            reason_codes=all_reasons,
        )

    def _unknown_state(self, symbol_scope: str, now: float, reasons: tuple) -> RegimeState:
        """§8 safe fallback. ALWAYS BLOCK new trades, zero exposure hint."""
        empty_dims = DimensionScores(reason_codes=reasons)
        empty_probs = RegimeProbabilities(values={})
        empty_compat = StrategyCompatibility(values={})
        return RegimeState(
            timestamp=now,
            symbol_scope=symbol_scope,
            market_regime=RegimeLabel.UNKNOWN,
            market_regime_confidence=0.0,
            market_regime_probabilities=empty_probs,
            short_term_regime=RegimeLabel.UNKNOWN,
            medium_term_regime=RegimeLabel.UNKNOWN,
            long_term_regime=RegimeLabel.UNKNOWN,
            regime_alignment_score=0.0,
            regime_stability_score=0.0,
            regime_duration_bars=0,
            previous_regime=None,
            transition_probability=0.0,
            transition_direction=TransitionDirection.NONE,
            dimensions=empty_dims,
            sector_regime=None,
            stock_regime=None,
            cross_level_alignment_score=None,
            strategy_compatibility=empty_compat,
            recommended_strategy=None,
            recommended_position_multiplier=0.0,
            trade_permission=TradePermission.BLOCK,
            data_freshness_ok=False,
            model_version=MODEL_VERSION,
            feature_version=FEATURE_VERSION,
            config_version=self.config.config_version,
            reason_codes=reasons,
        )

    # -- convenience accessors matching §38 interface names -------------
    def evaluate_regime_compatibility(self, strategy: str, state: RegimeState) -> float:
        return state.strategy_compatibility.values.get(strategy, 0.0)

    def get_position_multiplier(self, state: RegimeState) -> float:
        return state.recommended_position_multiplier

    def should_allow_new_trade(self, state: RegimeState, strategy: Optional[str] = None) -> TradePermission:
        if strategy is not None:
            compat = self.evaluate_regime_compatibility(strategy, state)
            if compat < 0.2 and state.trade_permission == TradePermission.ALLOW:
                return TradePermission.REDUCE
        return state.trade_permission

    def get_regime_context(self, state: RegimeState) -> dict:
        """Flat dict for logging / API responses (§55 schema)."""
        return {
            "market_regime": state.market_regime.value,
            "confidence": state.market_regime_confidence,
            "short_term_regime": state.short_term_regime.value,
            "medium_term_regime": state.medium_term_regime.value,
            "long_term_regime": state.long_term_regime.value,
            "regime_alignment_score": state.regime_alignment_score,
            "regime_stability": state.regime_stability_score,
            "regime_duration": state.regime_duration_bars,
            "trend_state": state.dimensions.trend_state.value,
            "volatility_state": state.dimensions.volatility_state.value,
            "breadth_state": state.dimensions.breadth_state.value,
            "momentum_state": state.dimensions.momentum_state.value,
            "stress_level": state.dimensions.stress_level.value,
            "transition_probability": state.transition_probability,
            "transition_direction": state.transition_direction.value,
            "strategy_compatibility": dict(state.strategy_compatibility.values),
            "recommended_strategy": state.recommended_strategy,
            "position_multiplier": state.recommended_position_multiplier,
            "trade_permission": state.trade_permission.value,
            "reason_codes": list(state.reason_codes),
            "model_version": state.model_version,
            "feature_version": state.feature_version,
            "config_version": state.config_version,
        }
