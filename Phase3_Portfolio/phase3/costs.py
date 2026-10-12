"""
Cost Model boundary.

Capital Feasibility's pure core takes a `price_fn(qty) -> TradeCapitalRequirement`
callback precisely so it never hard-depends on the Cost Model package.
Phase 3 is the layer that supplies that callback, and it supports two
sources:

  CostModelProvider          — the real TradeX Cost Model
                               (cost_model.engine.CostEngine). Use this in
                               live trading and backtesting. It delegates
                               to the adapter Capital Feasibility already
                               ships (adapters/cost_adapter.price_buy_leg),
                               so there is exactly one place that knows
                               how to build a cost_model.TradeInput.

  ApproximateZerodhaCostProvider
                             — a clearly-labelled STAND-IN for wiring and
                               testing Phase 3 before the Cost Model is
                               connected. It is an approximation of the
                               Zerodha equity charge sheet, NOT an
                               authority, and every result it produces is
                               stamped with a version string beginning
                               "APPROX-" so an approximate cost can never
                               be mistaken for a priced one downstream.

Both fail closed: an input they cannot price raises
CostModelIntegrationError rather than returning a zero cost, matching
capital_feasibility spec §29.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Protocol

from capital_feasibility.adapters.cost_adapter import price_buy_leg as _cost_model_price_buy_leg
from capital_feasibility.exceptions import CostModelIntegrationError
from capital_feasibility.models import LiquidityContext, TradeCapitalRequirement

_PAISE = Decimal("0.01")


def _round(x: Decimal) -> Decimal:
    return x.quantize(_PAISE, rounding=ROUND_HALF_UP)


class CostProvider(Protocol):
    """What Phase 3 needs from a cost source. Buy leg only: Capital
    Feasibility asks "what does it cost to ENTER", never round-trip."""

    def price_buy_leg(
        self,
        symbol: str,
        exchange: str,
        trade_type: str,
        entry_price: Decimal,
        quantity: int,
        liquidity: Optional[LiquidityContext] = None,
        same_day_cnc_square_off: bool = False,
        available_capital: Optional[Decimal] = None,
    ) -> TradeCapitalRequirement:
        ...


@dataclass
class CostModelProvider:
    """Wraps the real Cost Model. `cost_models` is the cost_model.models
    MODULE itself (not an instance) — that is what the shipped adapter
    expects."""

    cost_engine: object
    cost_models: object

    def price_buy_leg(
        self,
        symbol: str,
        exchange: str,
        trade_type: str,
        entry_price: Decimal,
        quantity: int,
        liquidity: Optional[LiquidityContext] = None,
        same_day_cnc_square_off: bool = False,
        available_capital: Optional[Decimal] = None,
    ) -> TradeCapitalRequirement:
        return _cost_model_price_buy_leg(
            self.cost_engine,
            self.cost_models,
            symbol,
            exchange,
            trade_type,
            entry_price,
            quantity,
            liquidity=liquidity,
            same_day_cnc_square_off=same_day_cnc_square_off,
            available_capital=available_capital,
        )


@dataclass
class ApproximateZerodhaCostProvider:
    """Stand-in buy-leg pricer for Indian equity on Zerodha.

    Modelled charges (ENTRY leg only):

      brokerage        DELIVERY: 0. INTRADAY: min(flat_cap, rate * turnover).
      STT              DELIVERY: charged on buy. INTRADAY: charged on sell
                       only, so the buy leg carries none.
      exchange txn     percentage of turnover, exchange-specific.
      SEBI turnover    fixed rate on turnover.
      stamp duty       buy side only, higher for delivery than intraday.
      GST              on (brokerage + exchange txn + SEBI).
      slippage         slippage_bps of turnover, plus a crude participation
                       impact term when average daily traded value is known.

    The rates below are DEFAULTS, not gospel: they change by regulation
    and by broker. Override them explicitly rather than trusting the
    defaults for anything that touches real money, and replace this whole
    object with CostModelProvider as soon as the Cost Model is wired.
    """

    brokerage_rate_intraday: Decimal = Decimal("0.0003")     # 0.03%
    brokerage_cap_intraday: Decimal = Decimal("20.00")
    stt_rate_delivery_buy: Decimal = Decimal("0.001")        # 0.1%
    exchange_txn_rate: Decimal = Decimal("0.0000297")        # NSE equity
    exchange_txn_rate_bse: Decimal = Decimal("0.0000375")
    sebi_rate: Decimal = Decimal("0.000001")                 # Rs 10 / crore
    stamp_duty_rate_delivery: Decimal = Decimal("0.00015")   # 0.015% buy
    stamp_duty_rate_intraday: Decimal = Decimal("0.00003")   # 0.003% buy
    gst_rate: Decimal = Decimal("0.18")
    slippage_bps: Decimal = Decimal("10")                    # 0.10% of turnover
    impact_coefficient: Decimal = Decimal("0.5")
    version: str = "APPROX-ZERODHA-EQUITY-1"

    def price_buy_leg(
        self,
        symbol: str,
        exchange: str,
        trade_type: str,
        entry_price: Decimal,
        quantity: int,
        liquidity: Optional[LiquidityContext] = None,
        same_day_cnc_square_off: bool = False,
        available_capital: Optional[Decimal] = None,
    ) -> TradeCapitalRequirement:
        if quantity <= 0:
            return TradeCapitalRequirement(
                quantity=0,
                position_value=Decimal("0"),
                execution_cost=Decimal("0"),
                required_capital=Decimal("0"),
                cost_model_version=self.version,
            )
        if entry_price is None or entry_price <= 0:
            raise CostModelIntegrationError(f"cannot price {symbol}: entry_price={entry_price!r}")
        trade_type = (trade_type or "").upper()
        if trade_type not in ("INTRADAY", "DELIVERY"):
            raise CostModelIntegrationError(f"unknown trade_type {trade_type!r}")
        exchange = (exchange or "").upper()
        if exchange not in ("NSE", "BSE"):
            raise CostModelIntegrationError(f"unknown exchange {exchange!r}")

        turnover = entry_price * Decimal(quantity)
        intraday = trade_type == "INTRADAY" or same_day_cnc_square_off

        brokerage = (
            min(self.brokerage_cap_intraday, self.brokerage_rate_intraday * turnover)
            if intraday
            else Decimal("0")
        )
        stt = Decimal("0") if intraday else self.stt_rate_delivery_buy * turnover
        txn_rate = self.exchange_txn_rate_bse if exchange == "BSE" else self.exchange_txn_rate
        exchange_txn = txn_rate * turnover
        sebi = self.sebi_rate * turnover
        stamp = (self.stamp_duty_rate_intraday if intraday else self.stamp_duty_rate_delivery) * turnover
        gst = self.gst_rate * (brokerage + exchange_txn + sebi)

        slippage = (self.slippage_bps / Decimal("10000")) * turnover
        impact = Decimal("0")
        if liquidity is not None and liquidity.average_daily_traded_value:
            adv = liquidity.average_daily_traded_value
            if adv > 0:
                participation = turnover / adv
                impact = self.impact_coefficient * participation * turnover

        execution_cost = _round(brokerage + stt + exchange_txn + sebi + stamp + gst + slippage + impact)
        position_value = _round(turnover)

        return TradeCapitalRequirement(
            quantity=quantity,
            position_value=position_value,
            execution_cost=execution_cost,
            required_capital=_round(position_value + execution_cost),
            cost_model_version=self.version,
        )
