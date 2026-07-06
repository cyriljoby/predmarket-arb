"""Small parsing helpers shared by the feed adapters."""

from __future__ import annotations

import re
from datetime import datetime, timezone

# An outright's entity must be a proper subject (person/team/place), not a
# threshold/scalar bucket ("Above 13000", "1+ wins", "below 7.60") or a bare
# Yes/No. Both feeds reject a FuturesEvent whose entity matches this.
_NON_ENTITY_RE = re.compile(
    r"^\s*(yes|no|above|below|under|over|at\s+least|at\s+most|more\s+than|"
    r"less\s+than|fewer\s+than|exactly|between|[<>$]|\d|\+)",
    re.IGNORECASE,
)


def is_entity(text: str) -> bool:
    """True if `text` names a proper outright subject, not a threshold/Yes-No."""
    t = (text or "").strip()
    return bool(t) and not _NON_ENTITY_RE.match(t) and "%" not in t


def to_float(x) -> float | None:
    return float(x) if x is not None else None


def parse_iso_dt(s: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (with a trailing 'Z') into tz-aware UTC."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
