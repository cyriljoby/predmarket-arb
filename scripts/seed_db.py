"""Seed the database from a live catalog fetch plus the current match set.

Run after every match refresh:
    .venv/bin/python scripts/seed_db.py
"""

import asyncio
import json

import aiohttp

from pmarb.config import MATCH_LOG_PATH
from pmarb.credentials import PolymarketUSCredentials
from pmarb.db import connect
from pmarb.db.schema import ensure_partitions
from pmarb.db.sync import upsert_markets, upsert_match_pairs
from pmarb.feeds.kalshi import KalshiFeed
from pmarb.feeds.polymarket import PolymarketUSFeed


async def main() -> None:
    matches = json.load(open(MATCH_LOG_PATH))
    try:
        reviews = json.load(open("reviews.json"))
    except FileNotFoundError:
        reviews = []
    need = ({m["kalshi_id"] for m in matches}
            | {m["polymarket_id"] for m in matches})

    creds = PolymarketUSCredentials.from_env()
    async with aiohttp.ClientSession() as s:
        kalshi, poly = await asyncio.gather(
            KalshiFeed(s).fetch_markets(),
            PolymarketUSFeed(s, creds).fetch_markets(),
        )
    markets = [m for m in kalshi + poly if m.id in need]
    print(f"catalogs: {len(kalshi)} Kalshi + {len(poly)} Poly; "
          f"{len(markets)} of {len(need)} referenced markets still listed")

    with connect() as conn:
        ensure_partitions(conn)
        n = upsert_markets(conn, markets)
        r = upsert_match_pairs(conn, matches, reviews)
        conn.commit()
    print(f"  markets   upserted {n}")
    print(f"  pairs     upserted {r['upserted']}, retracted {r['retracted']}, "
          f"skipped {r['skipped']} (market no longer listed)")


if __name__ == "__main__":
    asyncio.run(main())
