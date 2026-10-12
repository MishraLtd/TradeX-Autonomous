"""
PART — PUBLIC API (spec §31)

    result = capital_feasibility.evaluate(
        trade=trade, account=account, portfolio=portfolio,
        position_sizing=position_sizing, costs=costs, constraints=constraints
    )

adapted to TradeX's actual architecture: the three upstream components
are real objects from their own packages, not ad-hoc dicts. This module
is the ONLY place that imports cost_model / portfolio_manager /
position_sizing types by name — engine.py, gates.py, and max_quantity.py
stay dependency-free so they can be unit tested (and reused) without
those packages installed.
"""
from __future__ import annotations

from decimal import Decimal
from functools import partial
from typing import Optional

from .adapters.cost_adapter import price_buy_leg
from .adapters.portfolio_adapter import account_state_from_portfolio, asset_exposure_from_portfolio
from .adapters.position_sizing_adapter import trade_request_from_position_sizing
from .config import CapitalFeasibilityConfig, DEFAULT_CONFIG
from .engine import evaluate_trade
from .models import CapitalFeasibilityResult


def evaluate(
    sizing_request,          # position_sizing.models.PositionSizingRequest
    sizing_result,           # position_sizing.models.PositionSizeResult
    portfolio_state,         # portfolio_manager.portfolio_state.PortfolioState
    cost_engine,             # cost_model.engine.CostEngine instance
    cost_models_module,      # the cost_model.models module itself
    sector: Optional[str] = None,
    lot_size: int = 1,
    committed_capital: Optional[Decimal] = None,
    config: Optional[CapitalFeasibilityConfig] = None,
) -> CapitalFeasibilityResult:
    """The single entry point most callers should use: takes the real
    outputs of Position Sizing and the real Portfolio Manager state,
    prices the buy leg through the real Cost Model, and returns a
    CapitalFeasibilityResult. Never mutates any of its inputs."""
    config = config or DEFAULT_CONFIG

    trade_request = trade_request_from_position_sizing(
        sizing_request=sizing_request,
        sizing_result=sizing_result,
        sector=sector,
        lot_size=lot_size,
    )

    account_state = account_state_from_portfolio(
        portfolio_state=portfolio_state,
        config=config,
        committed_capital=committed_capital,
    )

    exposure = asset_exposure_from_portfolio(
        portfolio_state=portfolio_state,
        symbol=trade_request.symbol,
        sector=sector,
    )

    price_fn = partial(
        price_buy_leg,
        cost_engine,
        cost_models_module,
        trade_request.symbol,
        trade_request.exchange,
        trade_request.trade_type,
        trade_request.entry_price,
        liquidity=trade_request.liquidity,
        same_day_cnc_square_off=trade_request.same_day_cnc_square_off,
        available_capital=account_state.deployable_capital,
    )

    return evaluate_trade(
        request=trade_request,
        account_state=account_state,
        exposure=exposure,
        price_fn=price_fn,
        config=config,
    )
