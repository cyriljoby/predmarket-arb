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

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import websockets

from pmarb.config import (
    RECONNECT_BASE_SECONDS,
    RECONNECT_MAX_SECONDS,
    STREAM_IDLE_TIMEOUT_SECONDS,
    SUBSCRIPTION_ACK_TIMEOUT_SECONDS,
)
from pmarb.credentials import KalshiCredentials
from pmarb.feeds._util import is_entity as _is_entity
from pmarb.feeds._util import now_utc as _now_utc
from pmarb.feeds._util import parse_iso_dt as _parse_dt
from pmarb.feeds._util import to_float as _to_float
from pmarb.feeds.auth import kalshi_headers
from pmarb.feeds.base import SubscriptionMutationError
from pmarb.models import (
    FuturesEvent,
    LineEvent,
    Market,
    PriceLevel,
    PropEvent,
    SportsEvent,
)

_REST = "https://api.elections.kalshi.com/trade-api/v2"
_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
_WS_PATH = "/trade-api/ws/v2"

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

# Full-game SPREAD and TOTAL series, mapped to the same league keys the game
# matcher uses (Poly's slug league codes). Deliberately narrow: only the whole
# game. Kalshi also runs KXNCAAF1HTOTAL, KXNFL2QSPREAD, KXNCAAFTEAMTOTAL,
# KXMLBINNINGTOTAL and friends — a first-half total and a full-game total are
# different contracts, and pairing them would be the same class of error as
# matching "Stage 9" to the overall race.
_LINE_SERIES = {
    "KXNCAAFSPREAD": ("cfb", "spread"), "KXNCAAFTOTAL": ("cfb", "total"),
    "KXNFLSPREAD": ("nfl", "spread"), "KXNFLTOTAL": ("nfl", "total"),
    "KXMLBSPREAD": ("mlb", "spread"), "KXMLBTOTAL": ("mlb", "total"),
    "KXEPLSPREAD": ("epl", "spread"), "KXEPLTOTAL": ("epl", "total"),
    "KXLALIGASPREAD": ("lal", "spread"), "KXLALIGATOTAL": ("lal", "total"),
    "KXBUNDESLIGASPREAD": ("bun", "spread"), "KXBUNDESLIGATOTAL": ("bun", "total"),
    "KXLIGUE1SPREAD": ("lg1", "spread"), "KXLIGUE1TOTAL": ("lg1", "total"),
    "KXSERIEASPREAD": ("sea", "spread"), "KXSERIEATOTAL": ("sea", "total"),
    "KXUCLSPREAD": ("ucl", "spread"), "KXUCLTOTAL": ("ucl", "total"),
    "KXMLSSPREAD": ("mls", "spread"), "KXMLSTOTAL": ("mls", "total"),
}

# Player-prop series -> (league, stat). The stat vocabulary is shared with the
# Poly feed and closed on purpose: a stat only one venue names is not extracted,
# because "receiving yards" paired with "receptions" is two different bets on
# one player. Kalshi lists these as one market per (player, threshold) rung.
_PROP_SERIES = {
    "KXMLBTB": ("mlb", "total_bases"),
    "KXMLBHRR": ("mlb", "hits_runs_rbis"),
    "KXMLBHIT": ("mlb", "hits"),
    "KXMLBHR": ("mlb", "home_runs"),
    "KXMLBRBI": ("mlb", "rbis"),
    "KXNFLRECYDS": ("nfl", "receiving_yards"),
    "KXNFLREC": ("nfl", "receptions"),
    "KXNFLRSHYDS": ("nfl", "rush_yards"),
    "KXNFLPASSYDS": ("nfl", "pass_yards"),
    "KXNFLTD": ("nfl", "touchdowns"),
    # Pitcher props. Same rung shape, and Poly lists all four.
    "KXMLBKS": ("mlb", "strikeouts"),
    "KXMLBHA": ("mlb", "hits_allowed"),
    "KXMLBERA": ("mlb", "earned_runs_allowed"),
    "KXMLBWA": ("mlb", "walks_allowed"),
}

# "Kenneth Walker III: 15+" -> player and the integer threshold.
_PROP_YES_RE = re.compile(r"^(?P<player>.+?):\s*(?P<n>\d+(?:\.\d+)?)\+\s*$")

# "Vanderbilt wins by over 41.5 points" -> the team YES pays on covering.
_SPREAD_YES_RE = re.compile(r"^(?P<team>.+?)\s+wins by over\b", re.IGNORECASE)

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


def _line_event(market: dict, event: dict | None) -> LineEvent | None:
    """Spread/total identity for a market in a full-game line series, or None.

    The line itself is `floor_strike`, and every market in these series carries
    `strike_type: "greater"` (verified: 5,091/5,091 across the ten series) — so
    YES is always "over" and a market without that shape is refused rather than
    guessed at.

    Competitors come from the enclosing event title, which is the only place
    they appear: a total's own title is just "Over 35.5 points scored", naming
    neither team. The spread's YES team, by contrast, IS in `yes_sub_title`
    ("Vanderbilt wins by over 41.5 points"), which is what fixes orientation.
    """
    ticker = market.get("ticker", "")
    parts = ticker.split("-")
    mapped = _LINE_SERIES.get(parts[0])
    if mapped is None or event is None or len(parts) < 3:
        return None
    league, kind = mapped
    line = market.get("floor_strike")
    if line is None or market.get("strike_type") != "greater":
        return None
    title = event.get("title") or ""
    vs_part = next((p for p in title.split(":") if _VS_RE.search(p)), None)
    if vs_part is None:
        return None
    competitors = tuple(
        re.sub(r"\s+\d+$", "", s).strip()
        for s in _VS_RE.split(vs_part.strip(), maxsplit=1)
    )
    if len(competitors) != 2 or not all(competitors):
        return None
    yes_team = ""
    if kind == "spread":
        m = _SPREAD_YES_RE.match((market.get("yes_sub_title") or "").strip())
        if m is None:
            return None  # orientation unknown -> refuse, never assume a side
        yes_team = m["team"].strip()
    return LineEvent(
        league=league,
        start_time=_parse_event_start(parts[1]),
        competitors=competitors,  # type: ignore[arg-type]
        kind=kind,
        line=float(line),
        yes_team=yes_team,
        # "X wins by over L" is always X laying L points.
        yes_favored=True,
    )


def _prop_event(market: dict, event: dict | None) -> PropEvent | None:
    """Player-prop identity for a market in a prop series, or None.

    `yes_sub_title` carries both facts that matter ("Kenneth Walker III: 15+"),
    and `floor_strike` carries the same threshold in half-point form (14.5,
    strike_type greater). Both are read and CROSS-CHECKED: they encode one
    number two ways, so a disagreement means the wire format moved and the
    market must be refused rather than half-understood.

    The game comes from the enclosing event title ("Denver vs Kansas City:
    Receiving Yards"), the only place the two teams appear.
    """
    ticker = market.get("ticker", "")
    parts = ticker.split("-")
    mapped = _PROP_SERIES.get(parts[0])
    if mapped is None or event is None or len(parts) < 3:
        return None
    league, stat = mapped
    m = _PROP_YES_RE.match((market.get("yes_sub_title") or "").strip())
    if m is None or market.get("strike_type") != "greater":
        return None
    threshold = float(m["n"])
    floor = market.get("floor_strike")
    # "15+" must be the same rung as floor_strike 14.5. Tolerance is for float
    # representation only, not for disagreement.
    if floor is None or abs(float(floor) + 0.5 - threshold) > 1e-6:
        return None
    title = event.get("title") or ""
    vs_part = next((p for p in title.split(":") if _VS_RE.search(p)), None)
    if vs_part is None:
        return None
    competitors = tuple(
        re.sub(r"\s+\d+$", "", x).strip()
        for x in _VS_RE.split(vs_part.strip(), maxsplit=1)
    )
    player = m["player"].strip()
    if len(competitors) != 2 or not all(competitors) or not player:
        return None
    return PropEvent(
        league=league,
        start_time=_parse_event_start(parts[1]),
        competitors=competitors,  # type: ignore[arg-type]
        player=player,
        stat=stat,
        threshold=threshold,
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
        # Priority matters: a prop's yes_sub_title ("Cal Raleigh: 2+") reads as
        # an entity, so without this every one of the 6,331 prop markets was
        # classified as an OUTRIGHT and offered to the futures matcher.
        prop=(prop := None if game else _prop_event(market, event)),
        futures=None if (game or prop) else _futures_event(market, event),
        line=None if (game or prop) else _line_event(market, event),
    )


# --- the feed adapter ------------------------------------------------------ #
class KalshiFeed:
    """Kalshi market-data adapter. REST today; WebSocket streaming next."""

    platform = "kalshi"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: KalshiCredentials | None = None,
    ):
        self._session = session
        self._creds = credentials  # required only for stream_books (WS is authed)
        self._init_stream_state()

    def _init_stream_state(self) -> None:
        """Live-subscription state, owned by stream_books and read by resync.

        `_meta` is the authoritative desired set: the reader filters yields
        through it, and every (re)subscribe is built from it, so a mutation
        recorded here takes effect whether the socket is up or not.

        Called from stream_books as well as __init__ so a stream always starts
        from a clean slate (no ack waiter left over from a previous stream).
        """
        self._meta: dict[str, Market] = {}
        self._ws = None            # the open socket, or None between connects
        self._sid: int | None = None   # subscription id from the `subscribed` ack
        self._cmd_id = 1           # `id` counter; 1 is the initial subscribe
        self._acks: dict[int, asyncio.Future] = {}
        # Tickers a resync dropped, for the reader to forget. The maintained bid
        # ladders live in the reader's own `books` dict, which resync cannot
        # reach, and leaving a settled game's ladders there would grow memory by
        # a day's slate on every refresh.
        self._pruned: set[str] = set()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with self._session.get(
            f"{_REST}{path}", params=params, headers={"Accept": "application/json"}
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    # Runaway guard against a cursor that never terminates. Sits far above the
    # real catalog; hitting it is reported, never silently accepted.
    _MAX_PAGES = 1000

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
        cursor (defensive). `_MAX_PAGES` is a runaway guard, and hitting it
        is reported rather than silently returning a partial catalog.
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
        else:
            # Exhausted the guard while the cursor was still advancing — more
            # events exist than we fetched. A truncated catalog produces a
            # quietly incomplete match set, so say so.
            print(f"  WARNING {self.platform} discovery hit the {self._MAX_PAGES}-page "
                  f"guard at {len(markets)} markets — catalog is TRUNCATED")
        return markets

    async def stream_books(
        self, markets: list[Market], *, reconnect: bool = True,
        idle_timeout: float = STREAM_IDLE_TIMEOUT_SECONDS,
    ) -> AsyncIterator[Market]:
        """Continuously yield a fresh full-depth Market on every book update.

        Unlike Poly (full snapshots), Kalshi pushes an initial `orderbook_snapshot`
        per ticker then incremental `orderbook_delta`s, so this MAINTAINS book
        state: a snapshot replaces a ticker's bid ladders; a delta adjusts one
        (price, side) level by a signed size. After each message the affected
        ticker's book is re-normalized (asks derived from bids, as at REST) and
        yielded, carrying its discovery-time structured identity.

        `seq` is a single monotonic counter for the subscription; a gap means a
        dropped message, so we tear down and resubscribe (fresh snapshots). Also
        reconnects on a closed socket unless `reconnect=False`.
        """
        if self._creds is None:
            raise RuntimeError("Kalshi stream_books requires credentials")
        self._init_stream_state()
        meta = self._meta = {
            m.raw["market"]["ticker"]: m
            for m in markets
            if m.raw.get("market", {}).get("ticker")
        }

        backoff = RECONNECT_BASE_SECONDS
        while True:
            # Read INSIDE the loop: a resync may have mutated `_meta` while the
            # socket was down (or failed to mutate it live and asked for a
            # reconnect), and a resubscribe must use the corrected set.
            tickers = list(meta)
            # books: ticker -> {"yes": {price_str: size}, "no": {price_str: size}}
            books: dict[str, dict[str, dict[str, float]]] = {}
            last_seq: int | None = None
            # No socket until the connect below succeeds. Any mutation waiting on
            # an ack from the previous one will never get it, so it is failed
            # rather than left hanging — an unanswered mutation must surface as a
            # failure, never as optimism.
            self._ws, self._sid = None, None
            self._fail_pending_acks()
            try:
                headers = kalshi_headers(self._creds, "GET", _WS_PATH)
                async with websockets.connect(
                    _WS_URL, additional_headers=headers
                ) as ws:
                    self._ws, self._sid = ws, None
                    await ws.send(json.dumps({
                        "id": 1, "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta"],
                            "market_tickers": tickers,
                        },
                    }))
                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=idle_timeout)
                        except TimeoutError:
                            # Silence on a socket that never closed. Tear down
                            # rather than resume: the connection is discarded, so
                            # the cancelled recv() cannot leave it half-read, and
                            # resubscribing forces fresh snapshots.
                            print(f"  WARNING {self.platform} stream silent for "
                                  f"{idle_timeout:.0f}s — reconnecting")
                            break
                        # Stamped BEFORE the parse, so detection latency counts
                        # every microsecond this process spends on the update
                        # rather than starting the clock after the easy part.
                        received_mono = time.monotonic()
                        backoff = RECONNECT_BASE_SECONDS  # healthy stream -> reset
                        if self._pruned:
                            for gone in self._pruned:
                                books.pop(gone, None)
                            self._pruned.clear()
                        msg = json.loads(raw)
                        typ = msg.get("type")
                        # Tracked BEFORE the book-frame filter: control acks
                        # consume numbers in the same counter (verified live
                        # 2026-09-21 — `update_subscription` acks took seq 53
                        # and 54 between book frames 52 and 55), so advancing
                        # only on book frames reads those acks as a gap and
                        # resnapshots the whole subscription. Frames with no
                        # `seq` at all (the `subscribed` ack) don't count.
                        seq = msg.get("seq")
                        if seq is not None:
                            if last_seq is not None and seq != last_seq + 1:
                                break  # gap -> reconnect & resnapshot
                            last_seq = seq
                        if typ not in ("orderbook_snapshot", "orderbook_delta"):
                            # Control traffic. Two things here are load-bearing:
                            # the `sid` (without it no live mutation can name
                            # this subscription) and the reply to a mutation,
                            # which is the only proof the venue applied it.
                            if typ == "subscribed":
                                self._sid = (msg.get("msg") or {}).get("sid")
                            cmd_id = msg.get("id")
                            fut = (self._acks.pop(cmd_id, None)
                                   if cmd_id is not None else None)
                            if fut is not None and not fut.done():
                                fut.set_result(msg)
                            continue  # 'subscribed' ack, errors, etc.
                        body = msg.get("msg") or {}
                        ticker = body.get("market_ticker")
                        if ticker not in meta:
                            continue
                        book = books.setdefault(ticker, {"yes": {}, "no": {}})
                        if typ == "orderbook_snapshot":
                            book["yes"] = {p: float(s)
                                     for p, s in (body.get("yes_dollars_fp") or [])}
                            book["no"] = {p: float(s)
                                          for p, s in (body.get("no_dollars_fp") or [])}
                        else:  # orderbook_delta
                            side = body.get("side")
                            price = body.get("price_dollars")
                            levels = book.get(side)
                            if levels is None or price is None:
                                continue
                            levels[price] = levels.get(price, 0.0) + float(
                                body.get("delta_fp", 0.0)
                            )
                            if levels[price] <= 1e-9:
                                levels.pop(price, None)
                        ob = {
                            "yes_dollars": list(book["yes"].items()),
                            "no_dollars": list(book["no"].items()),
                        }
                        base = meta[ticker]
                        mk = normalize_orderbook(base.raw["market"], ob, _now_utc())
                        yield replace(mk, event=base.event, futures=base.futures,
                                      received_mono=received_mono)
            # ConnectionClosed = clean/keepalive drop; OSError = network/DNS/reset;
            # TimeoutError = a stalled connect/recv. All are recoverable.
            except (TimeoutError, websockets.ConnectionClosed, OSError):
                if not reconnect:
                    raise
                await asyncio.sleep(backoff)  # capped exponential backoff
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    # --- live subscription mutation ---------------------------------------- #
    def _fail_pending_acks(self) -> None:
        for fut in self._acks.values():
            if not fut.done():
                fut.set_exception(SubscriptionMutationError(
                    "connection closed before the venue acknowledged"))
        self._acks.clear()

    async def _mutate(self, ws, tickers: list[str], action: str,
                      timeout: float) -> None:
        """Send one `update_subscription` and VERIFY the venue applied it.

        Shape verified live 2026-09-21: the reply is
        `{"type":"ok","id":n,"sid":1,"seq":53,"msg":{"market_tickers":[...]}}`,
        where the list is the subscription's contents AFTER the change. Those
        acks consume numbers in the same `seq` counter as book frames, which is
        why the reader advances `last_seq` on control frames too (commit
        124ed19) — counting only book frames reads an ack as a gap and
        resnapshots all ~7,400 tickers.

        Raises SubscriptionMutationError on anything other than a reply that
        demonstrates the change, including silence. An unverified mutation is
        treated as a failure on purpose: the caller's fallback (reconnect with
        the corrected list) is cheap, and a refresh that believes it succeeded
        while the venue ignored it is the failure mode this whole path exists to
        avoid.
        """
        self._cmd_id += 1
        cmd_id = self._cmd_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._acks[cmd_id] = fut
        try:
            await ws.send(json.dumps({
                "id": cmd_id, "cmd": "update_subscription",
                "params": {"sids": [self._sid], "market_tickers": tickers,
                           "action": action},
            }))
            try:
                reply = await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError:
                raise SubscriptionMutationError(
                    f"{action} of {len(tickers)} tickers unacknowledged after "
                    f"{timeout:.0f}s") from None
        finally:
            self._acks.pop(cmd_id, None)
        if reply.get("type") != "ok":
            raise SubscriptionMutationError(f"{action} rejected: {reply}")
        # The echoed membership is checked when present: an `ok` that left the
        # set unchanged is the silent-no-op case, and it must not pass.
        echoed = (reply.get("msg") or {}).get("market_tickers")
        if echoed is None:
            return
        after = set(echoed)
        if action == "delete_markets" and (still := after & set(tickers)):
            raise SubscriptionMutationError(
                f"delete_markets acked but {len(still)} tickers are still "
                f"subscribed")
        if action == "add_markets" and (missing := set(tickers) - after):
            raise SubscriptionMutationError(
                f"add_markets acked but {len(missing)} tickers are absent")

    async def resync(
        self, markets: list[Market], *,
        ack_timeout: float = SUBSCRIPTION_ACK_TIMEOUT_SECONDS,
    ) -> dict:
        """Make the LIVE subscription cover exactly `markets`, no reconnect.

        Per-ticker mutation on an open socket is verified to work and to leave
        `seq` monotonic (probe 2026-09-21: 5/5 added tickers streamed
        immediately, 5/5 dropped went quiet, no sequence reset), which is what
        makes an in-process refresh worth doing at all — a restart would drop
        `last_append` and with it every open window's close marker.

        `_meta` is updated BEFORE anything is sent, for two reasons: a dropped
        ticker stops being yielded immediately even if the venue keeps sending
        it, and if the mutation fails the reconnect that follows resubscribes
        the corrected set rather than the stale one.

        Returns a summary with `applied`:
            "live"          mutated in place and verified;
            "reconnect"     the venue did not confirm; socket torn down so the
                            stream's own reconnect applies the new list;
            "on_reconnect"  no socket right now; the next connect picks it up.
        """
        desired = {
            m.raw["market"]["ticker"]: m
            for m in markets
            if m.raw.get("market", {}).get("ticker")
        }
        added = [t for t in desired if t not in self._meta]
        dropped = [t for t in self._meta if t not in desired]
        # Rebuild in place: `_meta` is captured by the running stream loop, so a
        # fresh dict would be written to a name nothing reads. Fresh metadata
        # wins for surviving tickers — venues amend titles and settlement dates.
        self._meta.clear()
        self._meta.update(desired)
        self._pruned.update(dropped)
        out = {"added": len(added), "dropped": len(dropped), "applied": "live"}
        if not added and not dropped:
            out["applied"] = "unchanged"
            return out

        ws, sid = self._ws, self._sid
        if ws is None or sid is None:
            out["applied"] = "on_reconnect"
            return out
        try:
            # Drop first: it is the half that frees the venue's resources, and
            # on Kalshi there is no cap to race, so order only matters for
            # keeping the subscription from briefly holding both sets.
            if dropped:
                await self._mutate(ws, dropped, "delete_markets", ack_timeout)
            if added:
                await self._mutate(ws, added, "add_markets", ack_timeout)
        except (SubscriptionMutationError, websockets.ConnectionClosed,
                OSError) as exc:
            print(f"  WARNING {self.platform} live resubscribe failed ({exc}); "
                  f"reconnecting with the corrected list "
                  f"(+{len(added)}/-{len(dropped)})")
            out["applied"] = "reconnect"
            # Closing makes the reader's recv raise, which lands in the stream's
            # own reconnect path — the same recovery a network drop takes, and
            # already proven by 14 days of uptime.
            with contextlib.suppress(Exception):
                await ws.close()
        return out
