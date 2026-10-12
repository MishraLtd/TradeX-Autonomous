from decimal import Decimal
from typing import List, Optional

from .audit import build_audit_trail, new_audit_id, now_utc
from .config import SizingConfig, DEFAULT_CONFIG
from .confidence import calculate_confidence_adjustment
from .constraints import apply_constraints
from .enums import (
    BindingConstraint,
    SizingConfidence,
    SizingLevel,
    SizingMethod,
    SizingStatus,
)
from .exceptions import InvalidRequestError, MissingCriticalInputError
from .expected_value import calculate_expected_value_size
from .hybrid import calculate_hybrid_size
from .kelly import calculate_kelly_size
from .liquidity import calculate_liquidity_adjustment
from .models import (
    CapitalFeasibilityConstraint,
    LiquiditySizingConstraint,
    PortfolioManagerInstruction,
    PortfolioRiskSizingConstraint,
    PositionSizeResult,
    PositionSizingRequest,
)
from .regime import calculate_regime_adjustment
from .risk_based import calculate_risk_based_size
from .rounding import round_quantity
from .validator import validate_request
from .volatility import calculate_volatility_adjustment


def _incomplete_result(request: PositionSizingRequest, reason: str, status: SizingStatus) -> PositionSizeResult:
    return PositionSizeResult(
        symbol=request.symbol,
        recommended_quantity=0,
        minimum_quantity=0,
        maximum_quantity=0,
        recommended_position_value=Decimal("0"),
        recommended_position_pct=None,
        risk_per_share=None,
        estimated_trade_risk=Decimal("0"),
        risk_pct=None,
        expected_gross_profit=None,
        expected_net_profit=None,
        expected_net_return=None,
        capital_efficiency=None,
        risk_efficiency=None,
        sizing_method=SizingMethod.RISK_BASED_HYBRID,
        sizing_level=SizingLevel.INCOMPLETE,
        sizing_components={},
        size_adjustments={},
        binding_constraints=[],
        sizing_status=status,
        rejection_reason=reason,
        confidence_adjustment=None,
        volatility_adjustment=None,
        liquidity_adjustment=None,
        regime_adjustment=None,
        portfolio_adjustment=None,
        sizing_confidence=SizingConfidence.LOW,
        model_versions=request.model_versions,
        calculation_timestamp=now_utc(),
        audit_id=new_audit_id(),
    )


def _determine_sizing_level(request: PositionSizingRequest) -> SizingLevel:
    has_stop = request.stop_loss is not None
    has_vol = request.atr_pct is not None
    has_conf = request.model_confidence is not None
    has_liq = request.average_traded_value is not None
    has_full_dist = (
        request.probability_of_profit is not None
        and request.expected_upside is not None
        and request.expected_downside is not None
    )
    if not has_stop:
        return SizingLevel.INCOMPLETE
    if has_stop and has_vol and has_conf and has_liq and has_full_dist:
        return SizingLevel.LEVEL_5_FULL
    if has_stop and has_vol and has_conf and has_liq:
        return SizingLevel.LEVEL_4_RISK_VOL_CONF_LIQ
    if has_stop and has_vol and has_conf:
        return SizingLevel.LEVEL_3_RISK_VOL_CONF
    if has_stop and has_vol:
        return SizingLevel.LEVEL_2_RISK_VOL
    return SizingLevel.LEVEL_1_RISK_ONLY


def _determine_sizing_confidence(sizing_level: SizingLevel) -> SizingConfidence:
    return {
        SizingLevel.LEVEL_5_FULL: SizingConfidence.HIGH,
        SizingLevel.LEVEL_4_RISK_VOL_CONF_LIQ: SizingConfidence.HIGH,
        SizingLevel.LEVEL_3_RISK_VOL_CONF: SizingConfidence.MEDIUM,
        SizingLevel.LEVEL_2_RISK_VOL: SizingConfidence.MEDIUM,
        SizingLevel.LEVEL_1_RISK_ONLY: SizingConfidence.LOW,
        SizingLevel.INCOMPLETE: SizingConfidence.LOW,
    }[sizing_level]


def calculate_position_size(
    request: PositionSizingRequest,
    capital_constraint: Optional[CapitalFeasibilityConstraint] = None,
    portfolio_risk_constraint: Optional[PortfolioRiskSizingConstraint] = None,
    portfolio_manager_instruction: Optional[PortfolioManagerInstruction] = None,
    liquidity_constraint: Optional[LiquiditySizingConstraint] = None,
    config: Optional[SizingConfig] = None,
) -> PositionSizeResult:
    config = config or DEFAULT_CONFIG

    # --- validation (fail closed) ---
    rejection = validate_request(request, portfolio_risk_constraint)
    if rejection is not None:
        status = (
            SizingStatus.NO_POSITION
            if portfolio_risk_constraint is not None and portfolio_risk_constraint.halt_sizing
            else SizingStatus.SIZING_INCOMPLETE
        )
        return _incomplete_result(request, rejection, status)

    try:
        base_size_raw, diagnostics = calculate_risk_based_size(
            request.entry_price,
            request.stop_loss,
            request.expected_total_cost,
            request.capital_available_for_sizing,
            request.max_risk_allowed,
            config,
        )
    except (InvalidRequestError, MissingCriticalInputError) as exc:
        return _incomplete_result(request, str(exc), SizingStatus.SIZING_INCOMPLETE)

    volatility_mult = calculate_volatility_adjustment(request.atr_pct, config)
    confidence_mult = calculate_confidence_adjustment(request.model_confidence, config)
    regime_mult = calculate_regime_adjustment(request.market_regime, config)

    portfolio_risk_mult = Decimal("1.0")
    if portfolio_risk_constraint is not None and portfolio_risk_constraint.risk_multiplier is not None:
        portfolio_risk_mult = max(Decimal("0"), min(Decimal("1.0"), portfolio_risk_constraint.risk_multiplier))

    hybrid = calculate_hybrid_size(
        base_size_raw, confidence_mult, volatility_mult, regime_mult, portfolio_risk_mult, config
    )

    liq_constraint = calculate_liquidity_adjustment(
        request.entry_price, request.average_traded_value, config, liquidity_constraint
    )

    cap_result = apply_constraints(
        size_after_adjustments=hybrid["size_after_adjustments_raw"],
        entry_price=request.entry_price,
        capital_available_for_sizing=request.capital_available_for_sizing,
        request_max_quantity=request.max_quantity,
        request_min_quantity=request.min_quantity,
        request_max_position_value=request.max_position_value,
        liquidity_constraint=liq_constraint,
        capital_constraint=capital_constraint,
        portfolio_instruction=portfolio_manager_instruction,
    )

    final_qty = round_quantity(
        cap_result["capped_qty_raw"],
        min_quantity=None,  # min handled explicitly below (one-share problem, Part 13/24)
        max_quantity=request.max_quantity,
        mode=config.default_rounding_mode,
    )

    binding_constraints: List[BindingConstraint] = [cap_result["binding_constraint"]]
    sizing_status = SizingStatus.APPROVED
    rejection_reason = None

    # --- one-share / small-account handling (Part 13, 24) ---
    # A multiplier that was deliberately driven to zero (regime halt, portfolio-risk
    # multiplier of 0, etc.) is a hard sizing-down-to-zero decision, not a rounding
    # artifact — it must NOT be rescued back up to 1 share.
    deliberately_zeroed = regime_mult == 0 or portfolio_risk_mult == 0
    if final_qty < 1 and not deliberately_zeroed:
        one_share_value = request.entry_price
        one_share_risk = diagnostics["net_risk_per_share"]
        fits_capital = one_share_value <= request.capital_available_for_sizing
        fits_risk = one_share_risk <= (request.max_risk_allowed or diagnostics["risk_budget"] * 10)
        # cost-to-edge check for a single share, if we have enough info
        cost_ok = True
        if request.expected_upside is not None and request.expected_total_cost is not None:
            if request.expected_upside > 0:
                cost_ratio = request.expected_total_cost / request.expected_upside
                cost_ok = cost_ratio <= config.max_cost_to_edge_ratio

        if fits_capital and fits_risk and cost_ok:
            final_qty = 1
            sizing_status = SizingStatus.REDUCED
            binding_constraints = [BindingConstraint.MIN_GRANULARITY]
        else:
            final_qty = 0
            sizing_status = SizingStatus.NO_POSITION
            rejection_reason = "Theoretical size below one share and 1 share fails hard/economic constraints"
    elif final_qty < 1 and deliberately_zeroed:
        final_qty = 0
        sizing_status = SizingStatus.NO_POSITION
        rejection_reason = "Size deliberately zeroed by regime/portfolio-risk multiplier"
        binding_constraints = [BindingConstraint.PORTFOLIO_RISK_HALT]
    elif final_qty < base_size_raw:
        sizing_status = SizingStatus.REDUCED

    # --- economic viability check (Part 25 / 63), applies even to qty >= 1 ---
    gross_profit_per_share = request.expected_upside
    net_profit_per_share = None
    if gross_profit_per_share is not None:
        net_profit_per_share = gross_profit_per_share - (request.expected_total_cost or Decimal("0"))

    expected_gross_profit = (Decimal(final_qty) * gross_profit_per_share) if (gross_profit_per_share is not None and final_qty > 0) else None
    expected_net_profit = (Decimal(final_qty) * net_profit_per_share) if (net_profit_per_share is not None and final_qty > 0) else None

    if (
        final_qty > 0
        and expected_net_profit is not None
        and expected_net_profit < config.min_expected_net_profit
    ):
        final_qty = 0
        sizing_status = SizingStatus.NO_POSITION
        rejection_reason = (
            f"Expected net profit ₹{expected_net_profit:.2f} below economic minimum "
            f"₹{config.min_expected_net_profit}"
        )
        binding_constraints = [BindingConstraint.ECONOMIC_MINIMUM]
        expected_gross_profit = None
        expected_net_profit = None

    position_value = Decimal(final_qty) * request.entry_price
    estimated_trade_risk = Decimal(final_qty) * diagnostics["gross_risk_per_share"]
    risk_pct = (estimated_trade_risk / request.capital_available_for_sizing) if final_qty > 0 else None
    position_pct = (position_value / request.capital_available_for_sizing) if final_qty > 0 else None

    capital_efficiency = (
        expected_net_profit / position_value if (expected_net_profit is not None and position_value > 0) else None
    )
    risk_efficiency = (
        expected_net_profit / estimated_trade_risk
        if (expected_net_profit is not None and estimated_trade_risk > 0)
        else None
    )

    ev_per_share = calculate_expected_value_size(
        request.probability_of_profit, request.probability_of_loss, request.expected_upside, request.expected_downside
    )
    kelly_diag = calculate_kelly_size(
        request.probability_of_profit,
        request.probability_of_loss,
        request.expected_upside,
        request.expected_downside,
        request.capital_available_for_sizing,
        request.entry_price,
        config,
    )

    sizing_level = _determine_sizing_level(request)
    sizing_confidence = _determine_sizing_confidence(sizing_level)

    # candidate min/max sensible quantities (Part 28) — base size before/after conservative haircut
    minimum_sensible = max(0, min(final_qty, round_quantity(base_size_raw * config.min_combined_mult, mode="DOWN")))
    maximum_sensible = max(final_qty, round_quantity(base_size_raw * config.max_vol_mult * config.max_confidence_mult, max_quantity=request.max_quantity, mode="DOWN"))

    sizing_components = {
        **{k: str(v) for k, v in diagnostics.items()},
        "ev_per_share": str(ev_per_share) if ev_per_share is not None else None,
        "kelly_diagnostics": {k: str(v) for k, v in kelly_diag.items()} if kelly_diag else None,
        "combined_mult": str(hybrid["combined_mult"]),
        "size_after_adjustments_raw": str(hybrid["size_after_adjustments_raw"]),
        "capped_qty_raw": str(cap_result["capped_qty_raw"]),
    }
    size_adjustments = {
        "confidence_mult": str(confidence_mult),
        "volatility_mult": str(volatility_mult),
        "regime_mult": str(regime_mult),
        "portfolio_risk_mult": str(portfolio_risk_mult),
    }

    audit_id = new_audit_id()
    audit_trail = build_audit_trail(
        request, config, diagnostics, size_adjustments, cap_result, final_qty, sizing_level, sizing_status
    )
    sizing_components["audit_trail"] = audit_trail
    sizing_components["audit_id"] = audit_id

    return PositionSizeResult(
        symbol=request.symbol,
        recommended_quantity=final_qty,
        minimum_quantity=minimum_sensible,
        maximum_quantity=maximum_sensible,
        recommended_position_value=position_value,
        recommended_position_pct=position_pct,
        risk_per_share=diagnostics["gross_risk_per_share"],
        estimated_trade_risk=estimated_trade_risk,
        risk_pct=risk_pct,
        expected_gross_profit=expected_gross_profit,
        expected_net_profit=expected_net_profit,
        expected_net_return=(expected_net_profit / position_value) if (expected_net_profit is not None and position_value > 0) else None,
        capital_efficiency=capital_efficiency,
        risk_efficiency=risk_efficiency,
        sizing_method=SizingMethod.RISK_BASED_HYBRID,
        sizing_level=sizing_level,
        sizing_components=sizing_components,
        size_adjustments=size_adjustments,
        binding_constraints=binding_constraints,
        sizing_status=sizing_status,
        rejection_reason=rejection_reason,
        confidence_adjustment=confidence_mult,
        volatility_adjustment=volatility_mult,
        liquidity_adjustment=Decimal(liq_constraint.liquidity_cap_quantity) if liq_constraint.liquidity_cap_quantity is not None else None,
        regime_adjustment=regime_mult,
        portfolio_adjustment=portfolio_risk_mult,
        sizing_confidence=sizing_confidence,
        model_versions=request.model_versions,
        calculation_timestamp=now_utc(),
        audit_id=audit_id,
    )
