"""
All tunable capital-feasibility parameters live here, as explicit
documented constants — never inline magic numbers elsewhere in the
package (spec §21). Matches the documentation style of
position_sizing/config.py and cost_model/config.py: every default states
its reasoning, not just its value, and is labelled as an "initial
assumption" where TradeX does not yet have live/backtest data to
calibrate against.

Neither the Portfolio Manager nor the Position Sizing package define
capital-ratio-based allocation/reserve limits — Portfolio Manager's
concentration thresholds (sector_concentration_threshold etc.) are
POSITION-COUNT-based, used for candidate scoring before a position size
exists (portfolio_manager/exposure.py). Capital-based concentration is a
genuinely new responsibility this model owns (spec §9, §21), so these
are new configuration values, not duplicates of anything upstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CapitalFeasibilityConfig:
    version: str = "1.0.0"

    # --- Capital reserve (spec §8) ---
    # Initial assumption: at ₹1,000-scale capital, an absolute rupee floor
    # matters more than a pure percentage (a 5% reserve on ₹1,000 is ₹50,
    # which is smaller than a single brokerage-capped intraday leg) — so
    # the reserve is the LARGER of an absolute floor and a percentage of
    # total capital, never just one or the other.
    minimum_cash_reserve: Decimal = Decimal("50.00")
    minimum_free_capital_ratio: Decimal = Decimal("0.05")   # 5% of total_capital

    # --- Position / trade allocation limits (spec §9, §21) ---
    # Fraction of TOTAL CAPITAL a single trade's position value may
    # consume, independent of any existing exposure to that symbol.
    # Initial assumption: at a ₹1,000 account with a soft preference for
    # up to ~5 concurrent positions (portfolio_manager.config
    # max_positions_preference), no single trade should structurally
    # crowd out the others.
    max_trade_capital_ratio: Decimal = Decimal("0.35")

    # Fraction of TOTAL CAPITAL that may be allocated to one symbol in
    # total (existing + this trade combined).
    max_asset_allocation_ratio: Decimal = Decimal("0.40")

    # Fraction of TOTAL CAPITAL that may be allocated to one sector in
    # total (existing + this trade combined). Slightly looser than the
    # single-asset limit since a sector can legitimately hold more than
    # one name.
    max_sector_allocation_ratio: Decimal = Decimal("0.60")

    # Portfolio-wide deployable-capital ceiling: fraction of TOTAL CAPITAL
    # that may be invested in aggregate (existing + this trade). Acts as
    # a backstop above the reserve ratio for accounts with unusual
    # committed-capital assumptions.
    max_portfolio_allocation_ratio: Decimal = Decimal("0.90")

    # --- Liquidity (spec §11) ---
    # Fraction of the average daily traded value a single order may
    # represent. Mirrors the spirit of position_sizing.config's
    # max_participation_pct (1.0%) but is intentionally a separate,
    # independently-tunable value: Position Sizing's cap is about not
    # moving the ideal size away from the risk-based number, this one is
    # about whether the trade can be filled at all without materially
    # distorting the market for an illiquid small-cap.
    liquidity_participation_limit: Decimal = Decimal("0.02")   # 2%

    # --- Lot sizing (spec §10) ---
    # Equity cash-market lots on NSE/BSE are 1 share; kept configurable
    # (not hard-coded in max_quantity.py) for any future segment where a
    # lot size > 1 applies (spec §40: do not assume fractional trading).
    default_lot_size: int = 1

    # --- Rounding / search ---
    # Ceiling on the number of Cost Model calls the binary search in
    # max_quantity.py may make per evaluation (spec §37 — bounded,
    # predictable cost for batch evaluation).
    max_binary_search_iterations: int = 40

    def reserved_capital(self, total_capital: Decimal) -> Decimal:
        return max(
            self.minimum_cash_reserve,
            (self.minimum_free_capital_ratio * total_capital).quantize(Decimal("0.01")),
        )


DEFAULT_CONFIG = CapitalFeasibilityConfig()
