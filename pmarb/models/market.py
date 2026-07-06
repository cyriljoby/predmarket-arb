"""The normalized Market schema — the shared data contract.

Every feed produces a Market; the matcher, detector, and logger consume it.
Nothing downstream of a feed should ever see platform-specific wire formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple


@dataclass(frozen=True, slots=True)
class SportsEvent:
    """Structured identity for head to head sports events, which are the only markets that can be reliably matched across venues.
    Allows matcher to algin markets with different phrasings, and detector to know which competitor is YES vs NO.
    """

    league: str
    start_time: datetime | None
    competitors: tuple[str, str]
    yes_competitor: str
    yes_abbrev: str = ""


@dataclass(frozen=True, slots=True)
class FuturesEvent:
    """Structured identity of an entity-outright market ("will ENTITY win/be X").

    Grounds tournament winners, championships, appointments, and "next team"
    markets in (competition, entity) so cross-venue matching is one-to-one
    instead of the lexical matcher's many-to-many text inflation. Both fields
    stored raw; the matcher normalizes. Class -> wire-field mapping (uniform
    across every outright class):

        field         Kalshi                     Polymarket US
        entity        market["yes_sub_title"]    market["title"]
        competition   enclosing event["title"]   market["question"]

    entity = the outright subject ("Ludvig Aberg"); competition = the shared
    question every entity answers ("Genesis Scottish Open Winner"). The Market's
    resolution_date + category disambiguate season/sport. Feeds return None for
    scalar/threshold markets ("At least 2.0%") — those aren't outrights.
    """

    entity: str
    competition: str
    entity_abbrev: str = ""


class PriceLevel(NamedTuple):
    """One level of order-book depth."""

    price: float
    size: float


@dataclass(frozen=True, slots=True)
class Market:
    """An immutable snapshot of one market on one venue.

    Immutable on purpose: feeds emit a *new* Market on every order-book update
    rather than mutating a shared object, which keeps the detector free of
    aliasing bugs across the two async feeds.

    Depth is ASK-side, the cost to buy, sorted best (cheapest) first.
    `yes_depth` answers "buy YES"; `no_depth` answers "buy NO". This is the only
    side the arb detector walks (a hedge buys both legs). 

    Ask-side depth (yes_depth/no_depth) is what the system trades on and walks for slippage.
    At each level: price is how much one share costs there, and size is how many shares are available at that price.
    
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
    match_aliases: tuple[str, ...] = ()  # alt phrasings the matcher also scores against
    # At most ONE is set: a market is a head-to-head game, an entity-outright,
    # or unstructured (both None -> lexical matcher).
    event: SportsEvent | None = None     # head-to-head game (moneyline markets)
    futures: FuturesEvent | None = None  # entity-outright (winner/next/appointment)

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
