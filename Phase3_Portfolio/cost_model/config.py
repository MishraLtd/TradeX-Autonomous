"""
PART 15 — LOW-CAPITAL TRADE GATE (configuration)
PART 20 — thresholds are deliberately centralised and documented with
their reasoning, and are all overridable by the caller (e.g. Portfolio
Manager) rather than hard-coded inside economics.py.

Reasoning behind each default (PART 15 explicitly forbids blindly
adopting example numbers without justification):

* MIN_NET_PROFIT_RUPEES = ₹2.00
    At ₹1,000 capital, a ₹1 "PASS" is statistically indistinguishable
    from the model's own rounding/estimation noise (individual charge
    components are rounded to the paisa, and slippage/impact are
    estimates with real uncertainty of low-single-digit rupees at this
    capital scale). Requiring a minimum ABSOLUTE rupee profit — not just
    a percentage — prevents the gate from waving through trades whose
    entire "edge" is smaller than the model's own error bars.

* MIN_NET_RETURN_PCT = 0.50%
    Below this, ordinary single-tick price noise in a ₹50-500 stock can
    erase the entire predicted edge before the order even reaches the
    exchange.

* MAX_COST_RATIO = 0.35 (35% of gross expected profit)
    If costs eat more than a third of the gross edge, the trade is
    disproportionately a bet on the broker/exchange/tax system rather
    than on the ML model's view. This is intentionally tighter than
    "50%" because at ₹1,000 capital the ABSOLUTE rupee edge is already
    tiny, so a percentage-only cost ratio is not protective enough on
    its own — it is used ALONGSIDE the absolute profit floor above, not
    instead of it.

* MAX_BREAK_EVEN_MOVE_PCT = 1.20%
    Chosen as roughly the reference daily volatility figure used in the
    market-impact model (1.5%) minus a margin — if the position needs to
    move further than a large fraction of a "normal" day's range just to
    reach break-even, the position is economically fragile.

* MIN_REQUIRED_SAFETY_BUFFER_PCT = 0.20%
    An explicit slack layered on top of measured friction (PART 10),
    covering model/estimation error that isn't otherwise captured.

* MAX_SLIPPAGE_TIER_FOR_AUTO_PASS
    Trades priced using LEVEL_1_FIXED / NO_LIQUIDITY_DATA_FALLBACK (i.e.
    the model had NO real market data and fell back to a blind
    conservative assumption) are allowed to pass the numeric checks but
    are flagged — callers with a stricter risk posture can choose to
    reject on this flag alone. This directly answers PART 15's
    "estimated_slippage uncertainty is too high -> REJECT" requirement
    without hard-coding a business decision that belongs to the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class GateConfig:
    min_net_profit_rupees: Decimal = Decimal("2.00")
    min_net_return_pct: Decimal = Decimal("0.50")
    max_cost_ratio: Decimal = Decimal("0.35")           # total_cost / gross_profit
    max_break_even_move_pct: Decimal = Decimal("1.20")
    min_required_safety_buffer_pct: Decimal = Decimal("0.20")
    reject_on_high_uncertainty_tier: bool = False        # caller can flip this to True for stricter behaviour
    high_uncertainty_tiers: tuple = (
        "LEVEL_1_FIXED", "NO_LIQUIDITY_DATA_FALLBACK",
    )
    # minimum position size below which per-order fixed costs (DP
    # charge, brokerage flat cap) structurally dominate — used to warn,
    # not to hard-reject, since it's capital-dependent.
    min_viable_position_value: Decimal = Decimal("300")


DEFAULT_GATE_CONFIG = GateConfig()
