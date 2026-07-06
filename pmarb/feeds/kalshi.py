"""Kalshi feed: REST market discovery + order-book normalization into Market.

Quarantines Kalshi's wire reality behind the MarketDataFeed interface:
  * markets come from the `/events?with_nested_markets=true` endpoint (the flat
    `/markets` list is swamped by auto-generated `KXMVE...` provisional markets);
  * prices/sizes are STRINGS in `_dollars`/`_fp` fields;
  * only BID ladders are published, so a YES *ask* is a NO *bid* at (1 - price).

The normalizer is a pure function (no network) so the derivation logic is fully
unit-testable against synthetic books.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from pmarb.feeds._util import is_entity as _is_entity
from pmarb.feeds._util import now_utc as _now_utc
from pmarb.feeds._util import parse_iso_dt as _parse_dt
from pmarb.feeds._util import to_float as _to_float
from pmarb.models import FuturesEvent, Market, PriceLevel, SportsEvent

_REST = "https://api.elections.kalshi.com/trade-api/v2"

# --- structured game identity ---------------------------------------------- #
# Head-to-head series whose event tickers encode the game (date, time, teams)
# and whose markets are one-per-competitor. Values are the canonical league
# keys the StructuredMatcher blocks on (Polymarket US's league tokens).
# Cricket formats are collapsed into one "cricket" block: the venues slice
# competitions differently (MLC vs T20 Blast vs internationals) but team names
# + start date disambiguate within the block.
_GAME_SERIES_LEAGUE = {
    "KXMLBGAME": "mlb",
    "KXNBAGAME": "nba",
    "KXNFLGAME": "nfl",
    "KXNHLGAME": "nhl",
    "KXWNBAGAME": "wnba",
    "KXNCAAFGAME": "cfb",
    "KXATPMATCH": "atp",
    "KXATPCHALLENGERMATCH": "atp",
    "KXWTAMATCH": "wta",
    "KXWTACHALLENGERMATCH": "wta",
    "KXITFMATCH": "itfme",
    "KXITFWMATCH": "itfwo",
    "KXUFCFIGHT": "ufc",
    "KXNPBGAME": "npb",
    "KXKBOGAME": "kbo",
    "KXVALORANTGAME": "valorant",
    "KXLOLGAME": "lol",
    "KXCS2GAME": "cs2",
    "KXDOTA2GAME": "dota2",
    "KXOWGAME": "overwatch",
    "KXR6GAME": "r6",
    "KXT20MATCH": "cricket",
    "KXWT20MATCH": "cricket",
    "KXODIMATCH": "cricket",
    "KXWODIMATCH": "cricket",
    "KXTESTMATCH": "cricket",
    "KXWTESTMATCH": "cricket",
}

# Event-ticker game segment: date, optional 4-digit start time, team codes.
# e.g. "26JUL081845HOUWSH" -> 2026-07-08 18:45 ET; "26SEP14DALSEA" -> date only.
_EVENT_SEG_RE = re.compile(
    r"^(?P<yy>\d{2})(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"(?P<dd>\d{2})(?P<hhmm>\d{4})?(?=[A-Z])"
)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
# Kalshi encodes game times in US Eastern (verified: ticker 1845 == 22:45Z close).
_EASTERN = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

_VS_RE = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)


def _parse_event_start(segment: str) -> datetime | None:
    """Game start (UTC) from an event-ticker game segment like "26JUL081845HOUWSH".

    Time-of-day is optional (weekly sports omit it); a date-only segment parses
    to midnight Eastern — coarse, but the matcher's day-level tolerance absorbs it.
    """
    m = _EVENT_SEG_RE.match(segment)
    if not m:
        return None
    hhmm = m["hhmm"] or "0000"
    try:
        local = datetime(
            2000 + int(m["yy"]), _MONTHS[m["mon"]], int(m["dd"]),
            int(hhmm[:2]), int(hhmm[2:]), tzinfo=_EASTERN,
        )
    except ValueError:
        return None
    return local.astimezone(_UTC)


def _sports_event(market: dict, event: dict | None) -> SportsEvent | None:
    """Structured game identity for a market in a game/match series, or None.

    Competitors come from the event title ("Houston vs Washington", "Pegula vs
    Gauff") — surnames or city names are fine, the matcher does subset matching
    against the other venue's full names. The YES competitor comes from
    `yes_sub_title`, which is always the full name.
    """
    ticker = market.get("ticker", "")
    parts = ticker.split("-")
    league = _GAME_SERIES_LEAGUE.get(parts[0])
    if league is None or event is None or len(parts) < 3:
        return None
    yes = (market.get("yes_sub_title") or "").strip()
    title = event.get("title") or ""
    # Keep only the "A vs B" segment when the title has colon-separated
    # decoration on either side ("OCS ...: Team Liquid vs. Dallas Fuel",
    # "France vs Morocco: Regulation Time Moneyline").
    vs_part = next((p for p in title.split(":") if _VS_RE.search(p)), None)
    if not yes or vs_part is None:
        return None
    sides = _VS_RE.split(vs_part.strip(), maxsplit=1)
    # Strip a trailing rematch counter ("McGregor vs. Holloway 2").
    competitors = tuple(re.sub(r"\s+\d+$", "", s).strip() for s in sides)
    if len(competitors) != 2 or not all(competitors):
        return None
    return SportsEvent(
        league=league,
        start_time=_parse_event_start(parts[1]),
        competitors=competitors,  # type: ignore[arg-type]
        yes_competitor=yes,
        yes_abbrev=parts[-1].lower(),
    )


def _futures_event(market: dict, event: dict | None) -> FuturesEvent | None:
    """Entity-outright identity for a market, or None.

    entity = `yes_sub_title` (full name); competition = the enclosing event
    title (the question every entity in the set shares). Returns None for plain
    Yes/No markets and scalar/threshold buckets (their `yes_sub_title` is
    "Yes"/"below 7.60"/"1+ ...", rejected by `_is_entity`).
    """
    if event is None:
        return None
    entity = (market.get("yes_sub_title") or "").strip()
    competition = (event.get("title") or "").strip()
    if not competition or not _is_entity(entity):
        return None
    return FuturesEvent(
        entity=entity,
        competition=competition,
        entity_abbrev=market.get("ticker", "").rsplit("-", 1)[-1].lower(),
    )


# --- pure helpers ---------------------------------------------------------- #
def _asks_from_bids(bid_levels: list) -> tuple[PriceLevel, ...]:
    """Convert one side's BID ladder into the OTHER side's ASK depth.

    A bid at price p (someone will buy that side at p) is an ask at (1 - p) for
    the opposite side, same size. Returned sorted cheapest-first, so the feed
    *guarantees* the ascending-depth invariant the detector relies on.
    """
    asks = [PriceLevel(round(1.0 - float(p), 4), float(s)) for p, s in bid_levels]
    return tuple(sorted(asks, key=lambda lvl: lvl.price))


def _best_bid(bid_levels: list) -> float | None:
    return max((float(p) for p, _ in bid_levels), default=None)


def normalize_orderbook(
    market: dict, orderbook_fp: dict, observed_at: datetime
) -> Market:
    """Build a full-depth Market from a Kalshi market dict + its `orderbook_fp`.

    `yes_depth` (cost to BUY yes) is derived from the NO bids; `no_depth` from
    the YES bids — because Kalshi publishes only bid ladders.
    """
    yes_bids = orderbook_fp.get("yes_dollars") or []
    no_bids = orderbook_fp.get("no_dollars") or []
    return Market(
        id=f"kalshi:{market['ticker']}",
        platform="kalshi",
        question=market.get("title", ""),
        resolution_date=_parse_dt(
            market.get("expiration_time") or market.get("close_time")
        ),
        category=market.get("category") or "",
        yes_depth=_asks_from_bids(no_bids),
        no_depth=_asks_from_bids(yes_bids),
        updated_at=observed_at,
        raw={"market": market, "orderbook_fp": orderbook_fp},
        yes_bid=_best_bid(yes_bids),
        no_bid=_best_bid(no_bids),
    )


def _market_metadata(
    market: dict, observed_at: datetime, event: dict | None = None
) -> Market:
    """A metadata-only Market (empty depth) for discovery/matching.

    `event` is the enclosing /events entry — its title names both competitors,
    which the per-market payload doesn't, so structured game identity can only
    be extracted at discovery time.
    """
    return Market(
        id=f"kalshi:{market['ticker']}",
        platform="kalshi",
        question=market.get("title", ""),
        resolution_date=_parse_dt(
            market.get("expiration_time") or market.get("close_time")
        ),
        category=market.get("category") or "",
        yes_depth=(),
        no_depth=(),
        updated_at=observed_at,
        raw={"market": market},
        yes_bid=_to_float(market.get("yes_bid_dollars")),
        no_bid=_to_float(market.get("no_bid_dollars")),
        event=(game := _sports_event(market, event)),
        # A game is never also an outright — only extract futures if not a game.
        futures=None if game else _futures_event(market, event),
    )


# --- the feed adapter ------------------------------------------------------ #
class KalshiFeed:
    """Kalshi market-data adapter. REST today; WebSocket streaming next."""

    platform = "kalshi"

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with self._session.get(
            f"{_REST}{path}", params=params, headers={"Accept": "application/json"}
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    _MAX_PAGES = 100  # safety cap (~20k events) against a runaway cursor loop

    @staticmethod
    def _is_tradeable(m: dict) -> bool:
        """Real, quotable market: not auto-generated multivariate, has a
        two-sided quote, and has a resolution date."""
        return (
            not m["ticker"].startswith("KXMVE")
            and m.get("yes_bid_dollars") is not None
            and m.get("yes_ask_dollars") is not None
            and bool(m.get("expiration_time") or m.get("close_time"))
        )

    async def fetch_markets(self) -> list[Market]:
        """Discover ALL active, tradeable markets, walking the events endpoint's
        cursor pagination. Returns metadata Markets (empty depth).

        Stops when a page returns no events, an empty cursor, or a repeated
        cursor (defensive), and is hard-capped at `_MAX_PAGES`.
        """
        now = _now_utc()
        markets: list[Market] = []
        cursor: str | None = None
        for _ in range(self._MAX_PAGES):
            params = {"status": "open", "with_nested_markets": "true", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/events", params=params)
            events = data.get("events", [])
            for event in events:
                markets.extend(
                    _market_metadata(m, now, event)
                    for m in event.get("markets", [])
                    if self._is_tradeable(m)
                )
            next_cursor = data.get("cursor")
            if not events or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return markets

    async def fetch_orderbook(self, market: Market) -> Market:
        """Fetch the live order book for a (metadata) Market and return a new
        full-depth Market snapshot. Kalshi's book endpoint is public (no auth)."""
        ticker = market.raw["market"]["ticker"]
        ob = await self._get(f"/markets/{ticker}/orderbook")
        return normalize_orderbook(
            market.raw["market"], ob.get("orderbook_fp", {}), _now_utc()
        )

    # stream_books(): the WebSocket streaming async generator is the next build
    # step. Omitted (rather than stubbed) so readers and the type checker see
    # exactly what is implemented today — REST discovery + book fetch.
