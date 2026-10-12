class MarketRegimeError(Exception):
    """Base exception. Any uncaught subclass must be handled by callers as
    'treat regime as UNKNOWN + BLOCK new trades' (§44) - never as 'skip and
    proceed with normal trading'."""


class InsufficientDataError(MarketRegimeError):
    """Not enough history to compute a feature (§20 min_bars_required)."""


class StaleDataError(MarketRegimeError):
    """Latest bar/tick is older than the configured freshness threshold."""


class FeatureOutOfBoundsError(MarketRegimeError):
    """A computed feature is outside any physically/statistically plausible
    range (e.g. negative ATR, ratio >1), signalling upstream data corruption."""


class ModelUnavailableError(MarketRegimeError):
    """The classifier (rule engine / stat / ML) could not produce a vote."""
