"""Small parsing helpers shared by the feed adapters."""

from __future__ import annotations

from datetime import datetime, timezone


def to_float(x) -> float | None:
    return float(x) if x is not None else None


def parse_iso_dt(s: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (with a trailing 'Z') into tz-aware UTC."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
