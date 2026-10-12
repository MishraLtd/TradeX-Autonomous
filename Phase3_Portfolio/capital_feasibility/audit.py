"""Audit id / timestamp helpers, mirroring position_sizing/audit.py."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_audit_id() -> str:
    return str(uuid.uuid4())
