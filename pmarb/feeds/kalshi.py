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

from datetime import datetime, timezone

import aiohttp

from pmarb.models import Market, PriceLevel

_REST = "https://api.elections.kalshi.com/trade-api/v2"


# --- pure helpers ---------------------------------------------------------- #
def _to_float(x) -> float | None:
    return float(x) if x is not None else None


def _parse_dt(s: str | None) -> datetime | None:
    """Parse a Kalshi ISO-8601 timestamp into a tz-aware UTC datetime."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


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


def _market_metadata(market: dict, observed_at: datetime) -> Market:
    """A metadata-only Market (empty depth) for discovery/matching."""
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
                    _market_metadata(m, now)
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
