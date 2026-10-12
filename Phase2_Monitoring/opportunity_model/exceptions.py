"""
Exceptions for the Opportunity Scoring Model.

Philosophy (spec §45 Failure-Safe Design): if critical information is
missing, invalid, or stale, the model must FAIL CLOSED. It must never
silently substitute a default (e.g. treating a missing risk estimate as
"zero risk"). Every failure mode below maps to an explicit, loggable
reason that flows into the OpportunityAssessment's rejection_reasons.
"""

from __future__ import annotations


class OpportunityModelError(Exception):
    """Base class for all Opportunity Model errors."""


class DataIncompleteError(OpportunityModelError):
    """
    Raised when a required upstream field is missing/None/NaN.

    This is the canonical "OPPORTUNITY_MODEL_DATA_INCOMPLETE" failure mode
    from spec §45. Callers (the Opportunity Engine loop) should catch this
    per-candidate, convert it into a REJECTED assessment with reason
    DATA_INCOMPLETE, and continue evaluating other candidates rather than
    crashing the whole ranking cycle.
    """


class StaleDataError(OpportunityModelError):
    """Raised when any input snapshot is older than its configured max age."""


class InvalidInputError(OpportunityModelError):
    """
    Raised for structurally invalid inputs that Pydantic validation itself
    could not catch generically (e.g. probability > 1, negative capital,
    NaN/Inf values that slipped through as floats).
    """


class ModelVersionMismatchError(OpportunityModelError):
    """
    Raised when an OpportunityAssessment is being reconstructed/replayed
    but the stored component model versions do not match the versions
    available at replay time, and exact reproduction was requested.
    """
