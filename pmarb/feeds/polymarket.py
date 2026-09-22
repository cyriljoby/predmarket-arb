"""Polymarket US feed: REST discovery + authenticated WebSocket order books.

Unlike Kalshi, Polymarket US has NO public REST order book — prediction-market
books come only from the authenticated WebSocket (`wss://api.polymarket.us/v1/ws/
markets`, subscribe by `marketSlug`). Each `marketData` message is a FULL snapshot
(not deltas) with `bids` (YES bids, desc) and `offers` (YES asks, asc), each level
`{"px": {"value", "currency"}, "qty"}`.

Normalization (pure): `yes_depth` is the `offers` ladder directly; `no_depth` is
derived from the YES `bids` at (1 - price) — the complementary side of a binary
market, the same trick the Kalshi feed uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime

import aiohttp
import websockets

from pmarb.config import (
    RECONNECT_BASE_SECONDS,
    RECONNECT_MAX_SECONDS,
    STREAM_IDLE_TIMEOUT_SECONDS,
    SUBSCRIBE_ERROR_GRACE_SECONDS,
    SUBSCRIPTION_ACK_TIMEOUT_SECONDS,
)
from pmarb.credentials import PolymarketUSCredentials
from pmarb.feeds._util import is_entity, now_utc, parse_iso_dt
from pmarb.feeds.auth import polymarket_us_headers
from pmarb.feeds.base import SubscriptionMutationError
from pmarb.models import (
    FuturesEvent,
    LineEvent,
    Market,
    PriceLevel,
    PropEvent,
    SportsEvent,
)

_GATEWAY = "https://gateway.polymarket.us"
_WS_URL = "wss://api.polymarket.us/v1/ws/markets"
_WS_PATH = "/v1/ws/markets"
# Polymarket US is Cloudflare-fronted and 1010-blocks non-browser User-Agents.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# Poly US futures/award markets share a GENERIC `question` ('FIFA World Cup
# Winner' for every team) but carry a rich `description` that names the entity.
# Strip the two boilerplate patterns: the "This market resolves to Yes if" lead-in
# and the ", scheduled ..." date tail (the date is matched via `endDate` instead).
_YES_IF_RE = re.compile(
    r'^\s*this market (?:will settle|resolves?|settles?)\s+to\s+"?yes"?\s+if\s+',
    re.IGNORECASE,
)
_SCHEDULED_RE = re.compile(r",\s*scheduled\b.*$", re.IGNORECASE)
# Below this, the "sentence" is an abbreviation ("Arizona St.", "A.J.") rather
# than a question. Chosen from the data: real first sentences run 12+ words.
_MIN_QUESTION_WORDS = 5


def _match_question(market: dict) -> str:
    """The best descriptive question for matching.

    Uses the description's first sentence (which names the entity, e.g. 'Will
    Spain win the 2026 FIFA World Cup'), stripped of boilerplate. Falls back to
    the raw question when there's no description. The FULL description is left
    untouched in `raw` for the resolution-verification step.
    """
    desc = (market.get("description") or "").strip()
    if not desc:
        return market.get("question", "")
    # A period is not reliably a sentence end here: the subjects themselves are
    # full of them. "Arizona St. advances to..." and "A.J. Brown records 1+
    # touchdowns..." both truncate to the abbreviation alone, which is exactly
    # the text a reviewer needs to judge the pair. Rather than enumerate
    # abbreviations, keep taking sentences until the result says something —
    # a real first sentence always clears the bar, an abbreviation never does.
    parts = re.split(r"(?<=[.?!])\s+", desc)
    first = ""
    for part in parts:
        first = f"{first} {part}".strip() if first else part
        if len(_YES_IF_RE.sub("", first).split()) >= _MIN_QUESTION_WORDS:
            break
    first = _YES_IF_RE.sub("", first)
    first = _SCHEDULED_RE.sub("", first).strip()
    return first or market.get("question", "")


def _team_name(team: dict) -> str:
    """The fullest name Poly gives for a competitor.

    Two fields carry it and neither is reliably the complete one. In college
    football `name` is the MASCOT ("Bulldogs") while `safeName` is the SCHOOL
    ("The Citadel") — and Kalshi names the school, so the mascot alone shares
    no token with it and the games cannot align at all. Elsewhere one contains
    the other ("Atlanta Braves"/"Braves", "Detroit Lions"/"Detroit"), where
    either alone already matches.

    So the two are joined only when neither contains the other, which is
    exactly the mascot-vs-school case. Joining unconditionally would duplicate
    tokens on every other league for nothing.
    """
    name = (team.get("name") or "").strip()
    safe = (team.get("safeName") or "").strip()
    if not safe or safe.lower() == name.lower():
        return name
    # Containment is checked on plain lowercase words rather than by importing
    # the matcher's tokenizer: a feed must not depend on the matching layer.
    # The two only need to agree on "is one of these a subset of the other",
    # which accent folding and single-char dropping do not change.
    n = frozenset(re.findall(r"[a-z0-9]+", name.lower()))
    sf = frozenset(re.findall(r"[a-z0-9]+", safe.lower()))
    if n <= sf or sf <= n:
        return name if len(n) >= len(sf) else safe
    return f"{safe} {name}"


def _sports_event(market: dict) -> SportsEvent | None:
    """Structured game identity for a moneyline market, or None.

    Poly US moneylines are categorical two-outcome markets (outcomes are the
    two competitors, not Yes/No). `marketSides` carries full team objects with
    `name`, `abbreviation`, and `league`; exactly one side is `long: true`,
    and the order book quotes THAT side (verified live: best offer == the long
    side's price). So Market.yes_depth means "buy the long competitor".
    """
    if market.get("marketType") != "moneyline":
        return None
    sides = market.get("marketSides") or []
    longs = [s for s in sides if s.get("long")]
    teams = [s.get("team") or {} for s in sides]
    if len(sides) != 2 or len(longs) != 1 or not all(t.get("name") for t in teams):
        return None
    long_team = (longs[0].get("team") or {})
    league = (long_team.get("league") or "").lower()
    # Cricket competitions collapse into one block (see Kalshi feed's map).
    if league.startswith("t20") or league in ("mlc", "odi", "test"):
        league = "cricket"
    if not league:
        return None
    return SportsEvent(
        league=league,
        start_time=parse_iso_dt(market.get("gameStartTime")),
        competitors=(_team_name(teams[0]), _team_name(teams[1])),
        yes_competitor=_team_name(long_team),
        yes_abbrev=(long_team.get("abbreviation") or "").lower(),
    )


# The two team names in a totals description. Totals carry NO team objects
# (`team` is null on all 19,344 sides), and their question uses mascots
# ("Eagles vs. Bearcats"), which share no token with Kalshi's school names.
# The description is the only place the full names appear, and it is present
# on 100% of them.
_TOTAL_TEAMS_RE = re.compile(
    r"\bif\s+(?P<a>.+?)\s+and\s+(?P<b>.+?)\s+(?:combine for|score)\b",
    re.IGNORECASE,
)


# The ONLY shapes that are a whole-game line. Poly slices every game into
# halves, quarters, and (baseball) the first five innings, and each slice is a
# separate contract — a first-half total paired with a full-game total is the
# same error as pairing "Stage 9" with the overall race. An allowlist rather
# than a "full_game" substring test, because `football_team_points_full_game_
# total` contains it and is a TEAM total (one team's points), which Kalshi
# lists under its own series and which this matcher does not cover.
_FULL_GAME_LINE_TYPES = frozenset({
    "football_team_full_game_spread", "football_team_full_game_total",
    "soccer_team_full_game_spread", "soccer_team_full_game_total",
    "baseball_team_full_game_spread", "baseball_team_full_game_total",
})


# Poly player-prop types -> the shared stat vocabulary (see the Kalshi feed's
# _PROP_SERIES; the two must name the same stats or nothing matches).
_PROP_TYPES = {
    "baseball_player_total_bases": ("mlb", "total_bases"),
    "baseball_player_hits_runs_rbis": ("mlb", "hits_runs_rbis"),
    "baseball_player_hits": ("mlb", "hits"),
    "baseball_player_home_runs": ("mlb", "home_runs"),
    "baseball_player_rbis": ("mlb", "rbis"),
    "football_player_receiving_yards": ("nfl", "receiving_yards"),
    "football_player_receptions": ("nfl", "receptions"),
    "football_player_rush_yards": ("nfl", "rush_yards"),
    "football_player_pass_yards": ("nfl", "pass_yards"),
    "football_player_touchdowns": ("nfl", "touchdowns"),
    "baseball_player_strikeouts": ("mlb", "strikeouts"),
    "baseball_player_hits_allowed": ("mlb", "hits_allowed"),
    "baseball_player_earned_runs_allowed": ("mlb", "earned_runs_allowed"),
    "baseball_player_walks_allowed": ("mlb", "walks_allowed"),
}

# "... in the Milwaukee Brewers vs Cincinnati Reds MLB game scheduled for ..."
# The trailing league/sport words are trimmed off the second team.
_PROP_GAME_RE = re.compile(
    r"\bin the (?P<a>.+?) vs\.? (?P<b>.+?)\s+"
    r"(?:MLB|NFL|NBA|NHL|professional football|professional baseball)\b",
    re.IGNORECASE,
)


def _prop_event(market: dict) -> PropEvent | None:
    """Player-prop identity for a Poly US market, or None.

    Poly states all four facts plainly: `title` is the player, `line` is the
    threshold, `sportsMarketType` is the stat, and the description names the
    game. YES is "at least `line`", the same rung Kalshi writes as "N+".
    """
    mapped = _PROP_TYPES.get(market.get("sportsMarketType"))
    if mapped is None or market.get("line") is None:
        return None
    league, stat = mapped
    player = (market.get("title") or "").strip()
    gs = parse_iso_dt(market.get("gameStartTime"))
    if not player or gs is None:
        return None
    sides = market.get("marketSides") or []
    longs = [x for x in sides if x.get("long")]
    # YES must be the "at least" side. Anything else and the contract is not
    # the one Kalshi writes.
    if len(sides) != 2 or len(longs) != 1 or \
            (longs[0].get("description") or "").strip().lower() != "yes":
        return None
    m = _PROP_GAME_RE.search(market.get("description") or "")
    if m is None:
        return None
    competitors = tuple(
        re.sub(r"^the\s+", "", x.strip(), flags=re.IGNORECASE)
        for x in (m["a"], m["b"])
    )
    if not all(competitors):
        return None
    return PropEvent(
        league=league,
        start_time=gs,
        competitors=competitors,
        player=player,
        stat=stat,
        threshold=float(market["line"]),
    )


def _line_event(market: dict) -> LineEvent | None:
    """Spread/total identity for a Poly US market, or None.

    Two shapes, and they differ in where the teams live:

      spreads — full `team` objects on BOTH sides (23,712/23,712 sides), so
        this reuses `_team_name` exactly as moneylines do. The long side's
        description carries the sign ("-4.50" / "+4.50") and the long team is
        the one YES pays on covering.

      totals — no team objects at all. Names come from the description prose,
        and YES is Over (the long side was "Over" on all 9,672 sampled).

    `line` is the venue's own numeric field, kept as an absolute value; which
    side of it YES sits on is carried by `yes_team` (spreads) or by kind
    (totals), never by the sign of this number.
    """
    kind = {"spreads": "spread", "totals": "total"}.get(market.get("marketType"))
    if kind is None or market.get("line") is None:
        return None
    if market.get("sportsMarketType") not in _FULL_GAME_LINE_TYPES:
        return None
    gs = parse_iso_dt(market.get("gameStartTime"))
    slug_parts = market.get("slug", "").split("-")
    league = slug_parts[1] if len(slug_parts) > 2 else ""
    if not league or gs is None:
        return None
    sides = market.get("marketSides") or []
    longs = [x for x in sides if x.get("long")]
    if len(sides) != 2 or len(longs) != 1:
        return None

    yes_favored = True
    if kind == "spread":
        teams = [x.get("team") or {} for x in sides]
        if not all(t.get("name") for t in teams):
            return None
        competitors = (_team_name(teams[0]), _team_name(teams[1]))
        yes_team = _team_name(longs[0].get("team") or {})
        # "-4.50" = the long team lays the points; "+4.50" = it receives them.
        desc = (longs[0].get("description") or "").strip()
        if desc.startswith("-"):
            yes_favored = True
        elif desc.startswith("+"):
            yes_favored = False
        else:
            return None   # unsigned side — the contract is unknown, refuse it
    else:
        m = _TOTAL_TEAMS_RE.search(market.get("description") or "")
        if m is None:
            return None  # a phrasing this parser does not know — never guess
        # "settles Yes if the Arizona Diamondbacks and Houston Astros
        # combine..." — the article is part of the sentence, not the name, and
        # it survives tokenization (3 chars), depressing every subset score.
        competitors = tuple(
            re.sub(r"^the\s+", "", x.strip(), flags=re.IGNORECASE)
            for x in (m["a"], m["b"])
        )
        # Over/Under rather than a team; refuse anything else rather than
        # assume the long side means Over.
        if (longs[0].get("description") or "").strip().lower() != "over":
            return None
        yes_team = ""
    if not all(competitors):
        return None
    return LineEvent(
        league=league.lower(),
        start_time=gs,
        competitors=competitors,
        kind=kind,
        line=abs(float(market["line"])),
        yes_team=yes_team,
        yes_favored=yes_favored,
    )


def _futures_event(market: dict) -> FuturesEvent | None:
    """Entity-outright identity for a Poly futures market, or None.

    entity = `title` (the outright subject); competition = `question` (the
    shared prompt). Only `marketType == "futures"`; None for threshold/scalar
    titles ("At least 2.0%"), which `is_entity` rejects.
    """
    if market.get("marketType") != "futures":
        return None
    entity = (market.get("title") or "").strip()
    competition = (market.get("question") or "").strip()
    if not competition or not is_entity(entity):
        return None
    # slug tail entity token, e.g. "...-w-ludabe" -> "ludabe"
    return FuturesEvent(
        entity=entity,
        competition=competition,
        entity_abbrev=market.get("slug", "").rsplit("-", 1)[-1].lower(),
    )


def _questions(
    market: dict, event: SportsEvent | None = None,
    line: LineEvent | None = None,
) -> tuple[str, tuple[str, ...]]:
    """The primary matching question plus any alias phrasings.

    Primary = description-derived (names the entity, readable — fixes generic
    futures questions). Alias = the raw question, kept because for game markets
    it's already a clean, concise phrasing that matches better than the verbose
    description. The matcher scores against both and takes the best.

    Moneyline questions aren't questions at all ("Max Holloway vs. Conor
    McGregor") and carry no YES semantics, so when the market has a structured
    `event` the primary is synthesized to say what YES actually pays on.
    """
    raw_q = market.get("question", "")
    if event is not None:
        a, b = event.competitors
        question = f"Will {event.yes_competitor} win {a} vs. {b}?"
        return question, (raw_q,) if raw_q else ()
    if line is not None:
        # Poly's own question narrates whichever side it likes — for
        # asc-nfl-den-kc-...-pos-5pt5 it reads "Kansas City Chiefs wins by more
        # than 5.5" while the quoted (long) side is DENVER +5.5. A reviewer
        # reading that question would check the wrong contract, so the primary
        # is synthesized to say what YES actually pays on.
        a, b = line.competitors
        if line.kind == "total":
            question = f"Will the total in {a} vs. {b} be over {line.line}?"
        else:
            sign = "-" if line.yes_favored else "+"
            question = (f"Will {line.yes_team} cover {sign}{line.line} "
                        f"in {a} vs. {b}?")
        return question, (raw_q,) if raw_q else ()
    primary = _match_question(market)
    aliases = (raw_q,) if raw_q and raw_q != primary else ()
    return primary, aliases


# --- pure normalization ---------------------------------------------------- #
def _levels(entries: list, price_of) -> tuple[PriceLevel, ...]:
    """Map raw WS levels into PriceLevels (sorted cheapest-first). `price_of`
    transforms the raw value (identity for asks, 1 - p for the derived side)."""
    levels = [
        PriceLevel(round(price_of(float(e["px"]["value"])), 4), float(e["qty"]))
        for e in entries
    ]
    return tuple(sorted(levels, key=lambda lvl: lvl.price))


def normalize_market_data(
    market: dict, market_data: dict, observed_at: datetime
) -> Market:
    """Build a full-depth Market from a Poly US market dict + a `marketData` msg."""
    bids = market_data.get("bids") or []      # YES bids, descending
    offers = market_data.get("offers") or []  # YES asks, ascending
    best_bid = max((float(b["px"]["value"]) for b in bids), default=None)
    best_offer = min((float(o["px"]["value"]) for o in offers), default=None)
    ev = _sports_event(market)
    pr = None if ev else _prop_event(market)
    ln = None if (ev or pr) else _line_event(market)
    question, aliases = _questions(market, ev, ln)
    return Market(
        id=f"polymarket_us:{market['slug']}",
        platform="polymarket_us",
        question=question,
        match_aliases=aliases,
        event=ev,
        futures=None if (ev or pr) else _futures_event(market),
        line=ln,
        prop=pr,
        resolution_date=parse_iso_dt(market.get("endDate")),
        category=market.get("category") or "",
        yes_depth=_levels(offers, lambda p: p),          # YES ask = offer directly
        no_depth=_levels(bids, lambda p: 1.0 - p),       # NO ask = YES bid at (1 - p)
        updated_at=observed_at,
        raw={"market": market, "marketData": market_data},
        yes_bid=best_bid,
        no_bid=round(1.0 - best_offer, 4) if best_offer is not None else None,
    )


def _market_metadata(market: dict, observed_at: datetime) -> Market:
    """A metadata-only Market (empty depth) for discovery/matching."""
    ev = _sports_event(market)
    pr = None if ev else _prop_event(market)
    ln = None if (ev or pr) else _line_event(market)
    question, aliases = _questions(market, ev, ln)
    return Market(
        id=f"polymarket_us:{market['slug']}",
        platform="polymarket_us",
        question=question,
        match_aliases=aliases,
        event=ev,
        futures=None if (ev or pr) else _futures_event(market),
        line=ln,
        prop=pr,
        resolution_date=parse_iso_dt(market.get("endDate")),
        category=market.get("category") or "",
        yes_depth=(),
        no_depth=(),
        updated_at=observed_at,
        raw={"market": market},
        yes_bid=None,
        no_bid=None,
    )


# --- live subscription state ------------------------------------------------ #
@dataclass
class _Shard:
    """One connection's subscriptions, and everything a resync needs to change
    them without reconnecting.

    `batches` maps requestId -> the slugs ONE subscribe call covered, because
    that is the removable unit: `unsubscribe` takes a requestId ALONE (verified
    2026-09-21 — adding subscriptionType/marketSlugs is rejected with
    `invalid_message`), so there is no per-slug removal to track instead.

    `meta_by_slug` is the shard's authoritative desired set: the reader filters
    yields through it and every resubscribe is built from it, so correcting it
    is what makes a refresh survive a failed mutation.
    """

    index: int
    meta_by_slug: dict[str, dict]
    batches: dict[str, list[str]] = field(default_factory=dict)
    ws: object | None = None          # open socket, or None between connects
    errors: int = 0                   # error frames seen; a resync watches this
    _rid_seq: int = 0

    def mint_request_id(self) -> str:
        """A requestId never reused on this shard: it is the handle an
        unsubscribe names, so a duplicate would make removal ambiguous."""
        self._rid_seq += 1
        return f"pmarb-s{self.index}-{self._rid_seq}"


# --- the feed adapter ------------------------------------------------------ #
class PolymarketUSFeed:
    """Polymarket US adapter: public REST discovery + authenticated WS books."""

    platform = "polymarket_us"
    _PAGE = 100
    # Runaway guard only — it must sit far above the real catalog (~22.4k
    # markets as of 2026-08). It used to be 200, which silently truncated
    # discovery at 20,000 and looked exactly like a completed fetch.
    _MAX_PAGES = 2000

    def __init__(
        self, session: aiohttp.ClientSession, credentials: PolymarketUSCredentials
    ):
        self._session = session
        self._creds = credentials
        self._init_stream_state()

    def _init_stream_state(self) -> None:
        """Live-stream state, owned by stream_books and read/mutated by resync.

        Called from stream_books too, so a stream always starts from a clean
        slate rather than inheriting a previous one's shards or ack waiters.
        """
        self._shards: list[_Shard] = []
        self._spawn_shard = None       # set while a stream is running
        self._acks: dict[str, asyncio.Future] = {}   # requestId -> ack waiter

    @staticmethod
    def _is_tradeable(m: dict) -> bool:
        """A live, binary (2-outcome) market with a slug and resolution date."""
        if m.get("closed") or not m.get("slug") or not m.get("endDate"):
            return False
        try:
            return len(json.loads(m.get("outcomes") or "[]")) == 2
        except (TypeError, json.JSONDecodeError):
            return False

    async def _get_gateway(self, path: str, params: dict) -> dict:
        async with self._session.get(
            f"{_GATEWAY}{path}",
            params=params,
            headers={"Accept": "application/json", "User-Agent": _UA},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def fetch_markets(self) -> list[Market]:
        """Discover ALL active, binary markets via offset pagination. Returns
        metadata Markets (empty depth) for the matcher."""
        now = now_utc()
        markets: list[Market] = []
        for page in range(self._MAX_PAGES):
            data = await self._get_gateway(
                "/v1/markets",
                {
                    "active": "true",
                    "closed": "false",
                    "limit": str(self._PAGE),
                    "offset": str(page * self._PAGE),
                },
            )
            batch = data.get("markets") or []
            markets.extend(
                _market_metadata(m, now) for m in batch if self._is_tradeable(m)
            )
            if len(batch) < self._PAGE:
                break
        else:
            # Loop ran to the guard without a short page, so the catalog is
            # larger than we fetched. Never fail silently here: a truncated
            # catalog produces a quietly incomplete match set.
            print(f"  WARNING {self.platform} discovery hit the {self._MAX_PAGES}-page "
                  f"guard at {len(markets)} markets — catalog is TRUNCATED")
        return markets

    # Max marketSlugs per subscribe message. The docs state a 100-markets-per-
    # subscription cap which this exceeds and which the venue silently tolerates
    # (verified again on 2026-09-21: 200-slug batches subscribe and stream).
    # Left at 200 deliberately — but it is also the REMOVAL GRANULARITY, because
    # `unsubscribe` is keyed by requestId alone and tears down whatever one
    # subscribe call covered. A bigger batch means more innocent slugs churned
    # when one of them settles.
    _SUB_BATCH = 200
    # HARD VENUE LIMIT, discovered live: the 2001st subscription on a
    # connection is refused with {"error": "max subscriptions per connection
    # reached"}. Asking for more does not fail the socket — the extra batches
    # are simply rejected, and a reader that only looks at `marketData` frames
    # (as this one did) sees silence indistinguishable from an idle market.
    # A run with 8,082 Poly markets therefore streamed 2,000 of them and
    # reported nothing wrong for two hours.
    #
    # Margin below the cap because the venue counts subscriptions, not markets,
    # and a reconnect can briefly overlap the old ones.
    #
    # `unsubscribe` DOES free slots (verified 2026-09-21: 198/200 slugs silenced,
    # then 200 brand-new slugs all streamed with no cap error), which is what
    # lets a refresh recycle inventory instead of accumulating dead games.
    _MAX_SUBS_PER_CONN = 1_900

    async def stream_books(
        self, markets: list[Market], *, reconnect: bool = True,
        idle_timeout: float = STREAM_IDLE_TIMEOUT_SECONDS,
    ) -> AsyncIterator[Market]:
        """Yield a fresh full-depth Market on every book update, across as many
        connections as the subscription cap requires.

        One connection holds at most `_MAX_SUBS_PER_CONN` markets, so a large
        match set is sharded and the shards are merged into one stream. Each
        shard reconnects independently: a drop costs that shard's books until it
        recovers, not the whole feed.

        Every shard — including a single one — runs as its own task feeding one
        queue. That uniformity is what lets `resync` start a NEW shard while the
        stream is live (the day the match set grows past the current shards'
        headroom) without a special case that only gets exercised in production.
        A shard that ends is surfaced rather than swallowed: its exception is
        re-raised here, which is how `reconnect=False` still propagates a closed
        socket to the caller.
        """
        self._init_stream_state()
        cap = self._MAX_SUBS_PER_CONN
        self._shards = [
            _Shard(i, self._meta_of(markets[j:j + cap]))
            for i, j in enumerate(range(0, max(len(markets), 1), cap))
        ]
        # Bounded: an unbounded queue would trade a silent subscription loss for
        # a silent memory leak. Poly sends full snapshots, so a dropped update is
        # superseded by the next one and the staleness gate covers the gap.
        queue: asyncio.Queue[Market] = asyncio.Queue(maxsize=10_000)
        tasks: list[asyncio.Task] = []

        async def pump(shard: _Shard) -> None:
            async for mk in self._stream_shard(
                    shard, reconnect=reconnect, idle_timeout=idle_timeout):
                await queue.put(mk)

        def spawn(shard: _Shard) -> None:
            """Attach one more connection to the live stream (used by resync)."""
            if shard not in self._shards:
                self._shards.append(shard)
            tasks.append(asyncio.create_task(pump(shard)))

        self._spawn_shard = spawn
        for sh in list(self._shards):
            tasks.append(asyncio.create_task(pump(sh)))
        if len(self._shards) > 1:
            print(f"  {self.platform}: {len(markets)} markets over "
                  f"{len(self._shards)} connections (cap {cap}/conn)")

        getter: asyncio.Task | None = None
        try:
            while True:
                if getter is None:
                    getter = asyncio.create_task(queue.get())
                if not tasks and queue.empty():
                    return          # every shard finished and nothing is pending
                await asyncio.wait([getter, *tasks],
                                   return_when=asyncio.FIRST_COMPLETED)
                # Queued books are drained BEFORE a finished shard is inspected,
                # so a shard that yielded and then died does not lose its last
                # update to its own exception.
                if getter.done():
                    mk = getter.result()
                    getter = None
                    yield mk
                    continue
                for t in [t for t in tasks if t.done()]:
                    tasks.remove(t)
                    if (exc := t.exception()) is not None:
                        raise exc
        finally:
            if getter is not None:
                getter.cancel()
            for t in tasks:
                t.cancel()
            self._shards, self._spawn_shard = [], None

    def _meta_of(self, markets: list[Market]) -> dict[str, dict]:
        """slug -> the raw market dict the normalizer needs."""
        return {
            m.raw["market"]["slug"]: m.raw["market"]
            for m in markets
            if m.raw.get("market", {}).get("slug")
        }

    async def _stream_shard(
        self, shard: _Shard, *, reconnect: bool = True,
        idle_timeout: float = STREAM_IDLE_TIMEOUT_SECONDS,
    ) -> AsyncIterator[Market]:
        """One connection's worth of books. Continuously yield a fresh
        full-depth Market on every book update.

        Holds ONE authenticated socket open, subscribes to every market's slug
        (batched by `_SUB_BATCH`), and yields a normalized Market per
        `marketData` message. Each Poly US message is a FULL snapshot, so there
        is no delta state to maintain — every yield is a complete book.

        On a dropped connection it reconnects and resubscribes (unless
        `reconnect=False`); the consumer's staleness gate covers the blind gap.
        Structured identity (event/futures) is recomputed from the market dict,
        so it survives streaming exactly as at discovery.

        The subscription set is `shard.meta_by_slug`, which `resync` mutates —
        so a resubscribe always sends the CURRENT set, and a mutation that the
        venue refused is corrected by the reconnect that follows it.
        """
        backoff = RECONNECT_BASE_SECONDS
        while True:
            headers = {
                **polymarket_us_headers(self._creds, "GET", _WS_PATH),
                "User-Agent": _UA,
            }
            shard.ws = None
            self._fail_pending_acks(shard)
            try:
                async with websockets.connect(
                    _WS_URL, additional_headers=headers
                ) as ws:
                    shard.ws = ws
                    # Fresh requestIds per connection: they are the only handle
                    # on a subscription, and reusing one across connections
                    # would make an `unsubscribe` ambiguous.
                    shard.batches.clear()
                    slugs = list(shard.meta_by_slug)
                    for i in range(0, len(slugs), self._SUB_BATCH):
                        await self._send_subscribe(
                            shard, slugs[i:i + self._SUB_BATCH])
                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=idle_timeout)
                        except TimeoutError:
                            # Silence on a socket that never closed. Tear down
                            # rather than resume: the connection is discarded, so
                            # the cancelled recv() cannot leave it half-read, and
                            # reconnecting resubscribes every slug.
                            print(f"  WARNING {self.platform} stream silent for "
                                  f"{idle_timeout:.0f}s — reconnecting")
                            break
                        # Stamped BEFORE the parse, so detection latency counts
                        # every microsecond this process spends on the update
                        # rather than starting the clock after the easy part.
                        received_mono = time.monotonic()
                        backoff = RECONNECT_BASE_SECONDS  # healthy stream -> reset
                        payload = json.loads(raw)
                        md = payload.get("marketData")
                        if md is None:
                            self._on_control(shard, payload)
                            continue
                        slug = md.get("marketSlug")
                        # Checked against the CURRENT set, so a slug dropped by a
                        # resync stops being yielded the moment it is dropped —
                        # even if the venue is still sending it, and even if the
                        # unsubscribe never landed.
                        if slug in shard.meta_by_slug:
                            yield replace(
                                normalize_market_data(
                                    shard.meta_by_slug[slug], md, now_utc()
                                ),
                                received_mono=received_mono,
                            )
            # ConnectionClosed = clean/keepalive drop; OSError = network/DNS/reset;
            # TimeoutError = a stalled connect/recv. All are recoverable.
            except (TimeoutError, websockets.ConnectionClosed, OSError):
                shard.ws = None
                if not reconnect:
                    raise
                await asyncio.sleep(backoff)  # capped exponential backoff
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    # --- control frames ---------------------------------------------------- #
    def _on_control(self, shard: _Shard, payload: dict) -> None:
        """Everything that is not a book update.

        Two frames matter. An `error` is the one that made a 75% subscription
        loss look like an idle market — never swallowed, and counted, because a
        resync reads that counter to decide whether its subscribes landed. An
        `unsubscribed` ack is the only positive confirmation this venue gives,
        so it resolves the waiting mutation.
        """
        if "error" in payload:
            shard.errors += 1
            print(f"  WARNING {self.platform} subscribe error: "
                  f"{payload['error']}")
            return
        rid = payload.get("requestId")
        if rid is not None and payload.get("unsubscribed"):
            fut = self._acks.pop(rid, None)
            if fut is not None and not fut.done():
                fut.set_result(payload)

    def _fail_pending_acks(self, shard: _Shard) -> None:
        """A mutation whose socket died never got an answer, and an unanswered
        mutation is a failed one — it must not resolve by optimism.

        Scoped to THIS shard by the requestId prefix: shards reconnect
        independently, and failing another connection's pending unsubscribe would
        recycle a shard that was perfectly healthy.
        """
        prefix = f"pmarb-s{shard.index}-"
        for rid in [r for r in self._acks if r.startswith(prefix)]:
            fut = self._acks.pop(rid)
            if not fut.done():
                fut.set_exception(SubscriptionMutationError(
                    "connection closed before the venue acknowledged"))

    async def _send_subscribe(self, shard: _Shard, slugs: list[str]) -> str:
        """Subscribe one batch and record it under its requestId.

        The requestId is remembered because it is the ONLY way to ever undo this
        subscription: `unsubscribe` takes a requestId and nothing else (sending
        subscriptionType/marketSlugs alongside it is rejected with
        `invalid_message` — the JSON->proto unmarshal refuses unknown fields).
        """
        rid = shard.mint_request_id()
        await shard.ws.send(json.dumps({"subscribe": {
            "requestId": rid,
            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
            "marketSlugs": slugs,
        }}))
        shard.batches[rid] = list(slugs)
        return rid

    async def _send_unsubscribe(self, shard: _Shard, rid: str,
                                timeout: float) -> None:
        """Tear down one subscribe batch and WAIT for the venue to confirm it.

        Verified shape (2026-09-21): `{"unsubscribe": {"requestId": rid}}` ->
        `{"requestId": rid, "unsubscribed": true}`. Silence raises: an
        unsubscribe that is not confirmed may have freed nothing, and a shard
        that believes it has headroom it does not have is how the cap bug
        started.
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._acks[rid] = fut
        try:
            await shard.ws.send(json.dumps({"unsubscribe": {"requestId": rid}}))
            try:
                await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError:
                raise SubscriptionMutationError(
                    f"unsubscribe {rid} unacknowledged after "
                    f"{timeout:.0f}s") from None
        finally:
            self._acks.pop(rid, None)

    # --- live subscription mutation ---------------------------------------- #
    async def resync(
        self, markets: list[Market], *,
        ack_timeout: float = SUBSCRIPTION_ACK_TIMEOUT_SECONDS,
        error_grace: float = SUBSCRIBE_ERROR_GRACE_SECONDS,
    ) -> dict:
        """Make the live subscriptions cover exactly `markets`, no reconnect.

        THE AWKWARD PART is granularity: there is no per-slug removal, so the
        removable unit is a whole subscribe batch. A batch that is only PARTLY
        dead is therefore unsubscribed entirely and its survivors immediately
        resubscribed under a new requestId. That costs those survivors a brief
        gap in their books — acceptable, because Poly sends full snapshots (the
        next message is a complete book, not a delta) and the detector's
        staleness gate discards the interval rather than trusting it. The
        alternative, leaving the batch alone, means never dropping a settled
        market that happens to share a batch with a live one, which is the stale
        -book artifact this refresh exists to remove.

        Slots are freed by the unsubscribe (verified), so adds are placed into
        whatever shards now have headroom under `_MAX_SUBS_PER_CONN`, and only
        the overflow starts a new shard.

        Returns a summary with `applied`: "live" when every mutation was
        confirmed, "recycled" when at least one shard fell back to reconnecting
        with the corrected list, "on_reconnect" when no shard was connected.
        """
        desired = self._meta_of(markets)
        current = {slug for sh in self._shards for slug in sh.meta_by_slug}
        added = [s for s in desired if s not in current]
        dropped = current - desired.keys()
        out = {"added": len(added), "dropped": len(dropped),
               "applied": "live", "recycled": 0, "new_shards": 0}
        if not self._shards:
            # Nothing is streaming, so there is nothing to mutate; the caller's
            # market list is what the next stream_books will subscribe.
            out["applied"] = "on_reconnect"
            return out
        if not added and not dropped:
            out["applied"] = "unchanged"
            return out

        recycled: list[_Shard] = []
        for shard in self._shards:
            if not await self._prune_shard(shard, desired, ack_timeout):
                recycled.append(shard)

        # Adds go to the shards with room, counting the room freed above.
        pending = list(added)
        for shard in self._shards:
            if not pending or shard in recycled:
                continue
            room = self._MAX_SUBS_PER_CONN - len(shard.meta_by_slug)
            take, pending = pending[:max(room, 0)], pending[max(room, 0):]
            for slug in take:
                shard.meta_by_slug[slug] = desired[slug]
            if not take or shard.ws is None:
                continue        # down shard: its reconnect subscribes the lot
            try:
                for i in range(0, len(take), self._SUB_BATCH):
                    await self._send_subscribe(
                        shard, take[i:i + self._SUB_BATCH])
            except (websockets.ConnectionClosed, OSError) as exc:
                print(f"  WARNING {self.platform} shard {shard.index} subscribe "
                      f"failed ({exc}); reconnecting with the corrected list")
                recycled.append(shard)

        # Overflow starts new connections. The venue counts subscriptions per
        # connection, so this is the only way to grow past the cap.
        while pending and self._spawn_shard is not None:
            chunk, pending = (pending[:self._MAX_SUBS_PER_CONN],
                              pending[self._MAX_SUBS_PER_CONN:])
            index = 1 + max((sh.index for sh in self._shards), default=-1)
            shard = _Shard(index, {s: desired[s] for s in chunk})
            self._spawn_shard(shard)
            out["new_shards"] += 1
        if pending:
            print(f"  WARNING {self.platform} {len(pending)} new markets could "
                  f"not be placed — no live stream to attach a shard to")

        # Poly acks an unsubscribe but says NOTHING on a successful subscribe;
        # only a later error frame reveals a refusal. So the only honest
        # verification is to watch for one, briefly, and recycle the shard if it
        # appears. Optimism here is what let 6,000 markets go unsubscribed for
        # two hours while every counter looked healthy.
        watched = [sh for sh in self._shards if sh not in recycled]
        before = {sh.index: sh.errors for sh in watched}
        await asyncio.sleep(error_grace)
        for shard in watched:
            if shard.errors > before[shard.index]:
                print(f"  WARNING {self.platform} shard {shard.index} reported "
                      f"an error after resubscribing; reconnecting with the "
                      f"corrected list")
                recycled.append(shard)

        for shard in recycled:
            await self._recycle(shard)
        out["recycled"] = len(recycled)
        if recycled:
            out["applied"] = "recycled"
        return out

    async def _prune_shard(self, shard: _Shard, desired: dict[str, dict],
                           ack_timeout: float) -> bool:
        """Drop everything on this shard that is no longer wanted.

        Returns False if the shard must be recycled instead (an unsubscribe the
        venue never confirmed, or a dead socket mid-flight). `meta_by_slug` is
        corrected FIRST either way, so both the reader's filter and any
        subsequent resubscribe use the new set regardless of what the venue did.
        """
        stale = {rid: slugs for rid, slugs in shard.batches.items()
                 if any(s not in desired for s in slugs)}
        for rid, slugs in stale.items():
            survivors = [s for s in slugs if s in desired]
            for slug in slugs:
                if slug not in desired:
                    shard.meta_by_slug.pop(slug, None)
            shard.batches.pop(rid, None)
            if shard.ws is None:
                continue        # the reconnect resubscribes the corrected set
            try:
                await self._send_unsubscribe(shard, rid, ack_timeout)
                if survivors:
                    await self._send_subscribe(shard, survivors)
            except (SubscriptionMutationError, websockets.ConnectionClosed,
                    OSError) as exc:
                print(f"  WARNING {self.platform} shard {shard.index} "
                      f"unsubscribe failed ({exc}); reconnecting with the "
                      f"corrected list")
                return False
        return True

    async def _recycle(self, shard: _Shard) -> None:
        """Fall back to a reconnect for one shard.

        Closing the socket makes its reader's recv raise, which lands in
        `_stream_shard`'s own reconnect path — the same recovery a network drop
        takes, and the one already proven by two weeks of uptime. Only this
        shard's books pause; the other connections and the Kalshi side are
        untouched.
        """
        ws, shard.ws = shard.ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
