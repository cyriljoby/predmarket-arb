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
    """Structured identity for a head-to-head game.

    Venues phrase moneylines incompatibly — Kalshi writes one market per team,
    Poly writes "A vs. B" with no YES semantics — so text similarity cannot pair
    them. Both encode the game in metadata, which is what this captures: enough
    to align the two listings, and to know which competitor YES pays on.
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


@dataclass(frozen=True, slots=True)
class LineEvent:
    """Structured identity of a SPREAD or TOTAL market on a single game.

    A moneyline asks who wins; these ask by how much, against a NUMBER. That
    number is the contract: a total at 45.5 and a total at 46.5 on the same
    game are different bets, so `line` is part of the identity, never a
    tolerance.

    Both venues encode it numerically — Kalshi as `floor_strike` with
    `strike_type: greater`, Poly as `line` — so this identity is exact
    arithmetic rather than the text similarity the lexical matcher needs.

    ORIENTATION is the subtle part and the reason `yes_team` exists. Kalshi
    writes one market per side ("Vanderbilt wins by over 41.5"), while Poly
    writes ONE market per (game, line) whose long side may be either the
    favorite (-41.5) or the underdog (+41.5) — measured across the live
    catalog, 4,046 groups were favorite-side and 4,477 underdog-side, never
    both. So YES means the same event on both venues only when `yes_team`
    agrees; when it names the opponent, the two YESes are complements and the
    hedge is inverted (see `MatchCandidate.poly_inverted`). Pairing those
    blindly would buy the SAME event twice while believing it was hedged.
    """

    league: str
    start_time: datetime | None
    competitors: tuple[str, str]
    kind: str                    # "spread" | "total"
    line: float                  # the number itself; exact equality is required
    # spread: the competitor whose covering YES pays on. total: "" (YES is
    # always Over on both venues — Kalshi writes "Over N points scored" and
    # Poly's long side was Over on all 9,672 markets sampled).
    yes_team: str = ""
    # spread only: is `yes_team` laying the points (-L, the favorite) or
    # receiving them (+L)? The SIGN is half the contract and cannot be
    # recovered from the team alone — "Denver -5.5" (Denver wins by more than
    # 5.5) and "Denver +5.5" (Denver loses by less than 5.5, or wins) are
    # different bets that share a team and a number. Kalshi only ever writes
    # the laying side ("Denver wins by over 5.5"), so it is always True there.
    yes_favored: bool = True


@dataclass(frozen=True, slots=True)
class PropEvent:
    """Structured identity of a PLAYER PROP ("will PLAYER reach N of STAT").

    The largest matchable block on either venue: Kalshi lists 6,331 of these
    across a dozen series and Poly's props are its single biggest market type.
    Both publish the same four facts — the game, the player, the statistic, and
    an integer threshold — so identity here is exact, like a line and unlike
    anything the lexical matcher handles.

    THRESHOLD CONVENTION. Both venues mean "at least N": Kalshi writes "15+"
    with `floor_strike` at the half-point below (14.5, strike_type greater),
    Poly writes `line: 15` with a gte question. This field stores N — the
    integer both venues name — so equality is direct and there is no push to
    reason about (a player cannot record 14.5 receptions).

    STAT is a closed vocabulary shared by both feeds. A stat that only one
    venue names is not extracted at all: "receiving yards" paired with
    "receptions" would be two different bets on one player.
    """

    league: str
    start_time: datetime | None
    competitors: tuple[str, str]   # the game, for verification
    player: str
    stat: str
    threshold: float               # N, meaning "at least N"


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

    Depth is ASK-side — the cost to buy — sorted cheapest first. `yes_depth`
    answers "buy YES", `no_depth` answers "buy NO", and each level is
    (price per share, shares available at that price). A hedge buys both legs,
    so the ask side is the only one the detector walks for slippage.
    """

    id: str                              # internal id, e.g. "kalshi:KXELONMARS-99"
    platform: str                        # "kalshi" | "polymarket"
    question: str                        # raw question text (used for matching)
    resolution_date: datetime            # when the market resolves
    # Fee-lookup key on Polymarket; informational only on Kalshi, whose fee is
    # a function of fill price rather than category.
    category: str
    yes_depth: tuple[PriceLevel, ...]    # ask-side depth to buy YES, cheapest first
    no_depth: tuple[PriceLevel, ...]     # ask-side depth to buy NO, cheapest first
    updated_at: datetime                 # when THIS snapshot was observed;
    #                                      drives the staleness gate
    # Original venue payload, kept for debugging and structured-id extraction.
    raw: dict = field(default_factory=dict, repr=False)
    yes_bid: float | None = None         # reference only — not used by detection
    no_bid: float | None = None          # reference only — not used by detection
    match_aliases: tuple[str, ...] = ()  # alt phrasings the matcher scores too
    # At most ONE is set: a market is a head-to-head game, an entity-outright,
    # or unstructured (both None -> lexical matcher).
    event: SportsEvent | None = None     # head-to-head game (moneyline markets)
    futures: FuturesEvent | None = None  # entity-outright (winner/next/appointment)
    # Spread/total on a game. A market carries at most ONE structured identity;
    # this is the third kind, and it is deliberately separate from `event`
    # because a spread is not a moneyline: the line is part of the contract.
    line: LineEvent | None = None
    # Player prop on a game. Fourth and last structured identity; like the
    # others, a market carries at most one.
    prop: PropEvent | None = None
    # Monotonic clock reading taken the instant this update's bytes came off the
    # socket, before any parsing. Paired with a second reading after detection,
    # it measures how long this stack takes to see an edge — the Δ the
    # window-survival question needs. MONOTONIC, not wall-clock: only
    # differences are ever taken, and NTP steps must not appear as latency.
    # None on REST/metadata snapshots, which never came off a stream.
    received_mono: float | None = None

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
