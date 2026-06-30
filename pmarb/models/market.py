"""The normalized Market schema — the shared data contract.

Every feed produces a Market; the matcher, detector, and logger consume it.
Nothing downstream of a feed should ever see platform-specific wire formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple


class PriceLevel(NamedTuple):
    """One level of order-book depth.

    A NamedTuple so it reads as `level.price` / `level.size`, while still being
    a plain (price, size) tuple — indexable and unpackable — for anything that
    expects the raw tuple form.
    """

    price: float
    size: float


@dataclass(frozen=True, slots=True)
class Market:
    """An immutable snapshot of one market on one venue.

    Immutable on purpose: feeds emit a *new* Market on every order-book update
    rather than mutating a shared object, which keeps the detector free of
    aliasing bugs across the two async feeds.

    Depth is ASK-side — the cost to BUY — sorted best (cheapest) first.
    `yes_depth` answers "buy YES"; `no_depth` answers "buy NO". This is the only
    side the arb detector walks (a hedge buys both legs). On Kalshi these ladders
    are *derived* from the venue's bid ladders (a YES ask == a NO bid at 1-price);
    that derivation lives in the feed, never here.

    `yes_ask` / `no_ask` are intentionally NOT stored — they are the top of the
    depth ladder, so they're exposed as properties to guarantee they can never
    drift out of sync with the depth they summarize.
    """

    id: str                              # internal id, e.g. "kalshi:KXELONMARS-99"
    platform: str                        # "kalshi" | "polymarket"
    question: str                        # raw question text (used for matching)
    resolution_date: datetime            # when the market resolves
    category: str                        # fee-lookup key (Polymarket); informational for Kalshi
    yes_depth: tuple[PriceLevel, ...]    # ask-side depth to buy YES, cheapest first
    no_depth: tuple[PriceLevel, ...]     # ask-side depth to buy NO, cheapest first
    updated_at: datetime                 # when THIS snapshot was observed (drives the staleness gate)
    raw: dict = field(default_factory=dict, repr=False)  # original payload, for debugging
    yes_bid: float | None = None         # reference only — not used by detection
    no_bid: float | None = None          # reference only — not used by detection

    @property
    def yes_ask(self) -> float | None:
        """Best (cheapest) ask to buy YES, or None if the book is empty."""
        return self.yes_depth[0].price if self.yes_depth else None

    @property
    def no_ask(self) -> float | None:
        """Best (cheapest) ask to buy NO, or None if the book is empty."""
        return self.no_depth[0].price if self.no_depth else None

    def age_seconds(self, now: datetime) -> float:
        """How stale this snapshot is relative to `now`, in seconds.

        The staleness *policy* (the threshold, what to do when exceeded) lives
        in the detector/config; this is just the measurement.
        """
        return (now - self.updated_at).total_seconds()
