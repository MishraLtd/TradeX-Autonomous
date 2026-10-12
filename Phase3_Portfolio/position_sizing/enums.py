from enum import Enum


class TradeType(str, Enum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"


class MarketRegime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    PANIC = "PANIC"
    UNKNOWN = "UNKNOWN"


class SizingStatus(str, Enum):
    APPROVED = "APPROVED"
    REDUCED = "REDUCED"
    NO_POSITION = "NO_POSITION"
    SIZING_INCOMPLETE = "SIZING_INCOMPLETE"


class SizingMethod(str, Enum):
    RISK_BASED = "RISK_BASED"
    RISK_BASED_HYBRID = "RISK_BASED_HYBRID"          # default live method
    EXPECTED_VALUE = "EXPECTED_VALUE"                  # comparison only
    FRACTIONAL_KELLY = "FRACTIONAL_KELLY"              # comparison only, hard-capped
    FIXED_FRACTION = "FIXED_FRACTION"                  # comparison / baseline only


class SizingLevel(str, Enum):
    """Fallback hierarchy — Part 45 of DESIGN.md. Recorded on every result."""
    LEVEL_1_RISK_ONLY = "LEVEL_1_RISK_ONLY"
    LEVEL_2_RISK_VOL = "LEVEL_2_RISK_VOL"
    LEVEL_3_RISK_VOL_CONF = "LEVEL_3_RISK_VOL_CONF"
    LEVEL_4_RISK_VOL_CONF_LIQ = "LEVEL_4_RISK_VOL_CONF_LIQ"
    LEVEL_5_FULL = "LEVEL_5_FULL"
    INCOMPLETE = "INCOMPLETE"


class SizingConfidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ScenarioType(str, Enum):
    BASE = "BASE"
    CONSERVATIVE = "CONSERVATIVE"
    STRESS = "STRESS"


class BindingConstraint(str, Enum):
    RISK_BUDGET = "RISK_BUDGET"
    CAPITAL_CAP = "CAPITAL_CAP"
    LIQUIDITY_CAP = "LIQUIDITY_CAP"
    PORTFOLIO_CAP = "PORTFOLIO_CAP"
    MAX_QUANTITY_CONFIG = "MAX_QUANTITY_CONFIG"
    MIN_GRANULARITY = "MIN_GRANULARITY"
    PORTFOLIO_RISK_HALT = "PORTFOLIO_RISK_HALT"
    ECONOMIC_MINIMUM = "ECONOMIC_MINIMUM"
    NONE = "NONE"
