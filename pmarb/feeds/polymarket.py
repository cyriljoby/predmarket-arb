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
import json
import re
from datetime import datetime

import aiohttp
import websockets

from pmarb.credentials import PolymarketUSCredentials
from pmarb.feeds._util import now_utc, parse_iso_dt
from pmarb.feeds.auth import polymarket_us_headers
from pmarb.models import Market, PriceLevel

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
    first = re.split(r"(?<=[.?!])\s+", desc, maxsplit=1)[0]
    first = _YES_IF_RE.sub("", first)
    first = _SCHEDULED_RE.sub("", first).strip()
    return first or market.get("question", "")


def _questions(market: dict) -> tuple[str, tuple[str, ...]]:
    """The primary matching question plus any alias phrasings.

    Primary = description-derived (names the entity, readable — fixes generic
    futures questions). Alias = the raw question, kept because for game markets
    it's already a clean, concise phrasing that matches better than the verbose
    description. The matcher scores against both and takes the best.
    """
    raw_q = market.get("question", "")
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
    return Market(
        id=f"polymarket_us:{market['slug']}",
        platform="polymarket_us",
        question=_questions(market)[0],
        match_aliases=_questions(market)[1],
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
    return Market(
        id=f"polymarket_us:{market['slug']}",
        platform="polymarket_us",
        question=_questions(market)[0],
        match_aliases=_questions(market)[1],
        resolution_date=parse_iso_dt(market.get("endDate")),
        category=market.get("category") or "",
        yes_depth=(),
        no_depth=(),
        updated_at=observed_at,
        raw={"market": market},
        yes_bid=None,
        no_bid=None,
    )


# --- the feed adapter ------------------------------------------------------ #
class PolymarketUSFeed:
    """Polymarket US adapter: public REST discovery + authenticated WS books."""

    platform = "polymarket_us"
    _PAGE = 100
    _MAX_PAGES = 200  # safety cap

    def __init__(
        self, session: aiohttp.ClientSession, credentials: PolymarketUSCredentials
    ):
        self._session = session
        self._creds = credentials

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
        return markets

    async def fetch_book(self, market: Market, timeout: float = 15.0) -> Market | None:
        """One-shot: open the authed WS, subscribe to this market's slug, take the
        first full snapshot, and normalize it. Returns None if no data arrives.

        (The continuous `stream_books()` generator that keeps one socket open for
        many slugs is the next build step; this proves the path end to end.)
        """
        meta = market.raw["market"]
        headers = {
            **polymarket_us_headers(self._creds, "GET", _WS_PATH),
            "User-Agent": _UA,
        }
        async with websockets.connect(_WS_URL, additional_headers=headers) as ws:
            await ws.send(
                json.dumps(
                    {
                        "subscribe": {
                            "requestId": "pmarb",
                            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                            "marketSlugs": [meta["slug"]],
                        }
                    }
                )
            )
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            md = json.loads(raw).get("marketData")
            return normalize_market_data(meta, md, now_utc()) if md else None
