"""Discover both venues, match, and export candidates for external review.

Writes two files:
  matches.json         - standard candidate records (for the manual review loop)
  matches_review.json  - enriched with each venue's RESOLUTION CRITERIA, formatted
                         for an LLM judge to decide true resolution matches

Run: .venv/bin/python scripts/export_candidates.py
"""

import asyncio
import json

import aiohttp

from pmarb.credentials import PolymarketUSCredentials
from pmarb.feeds.kalshi import KalshiFeed
from pmarb.feeds.polymarket import PolymarketUSFeed
from pmarb.matching.matcher import RuleBasedMatcher, write_matches


async def main() -> None:
    creds = PolymarketUSCredentials.from_env()
    async with aiohttp.ClientSession() as session:
        kalshi = await KalshiFeed(session).fetch_markets()
        poly = await PolymarketUSFeed(session, creds).fetch_markets()

    candidates = RuleBasedMatcher().match(kalshi, poly)
    write_matches(candidates)  # -> matches.json

    by_id = {m.id: m for m in kalshi + poly}
    review = []
    for c in candidates:
        k = by_id[c.kalshi_id].raw.get("market", {})
        p = by_id[c.polymarket_id].raw.get("market", {})
        review.append(
            {
                "similarity_score": c.similarity_score,
                "resolution_date_delta_days": c.resolution_date_delta_days,
                "kalshi": {
                    "id": c.kalshi_id,
                    "question": c.kalshi_question,
                    "rules_primary": k.get("rules_primary", ""),
                    "rules_secondary": k.get("rules_secondary", ""),
                },
                "polymarket_us": {
                    "id": c.polymarket_id,
                    "question": c.polymarket_question,
                    "description": p.get("description", ""),
                    "resolution_source": p.get("resolutionSource", ""),
                },
            }
        )
    with open("matches_review.json", "w") as f:
        json.dump(review, f, indent=2)

    print(f"candidates: {len(candidates)}")
    print("wrote matches.json and matches_review.json")


if __name__ == "__main__":
    asyncio.run(main())
