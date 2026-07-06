"""Run the detector on matched candidates against LIVE books.

Fetches Kalshi books (REST, concurrent) + Poly US books (one batched WS), runs
detect_pair on each candidate, and reports:
  - the apparent top-of-book edge distribution
  - how many clear fees+slippage (real arb) with their sizes
  - extreme apparent edges (likely mismatches — the anomaly signal)

Batch-snapshot, so the 2s staleness gate is disabled (legs aren't simultaneous
to the second — a caveat for precise arb, fine for a first measurement).

Run: .venv/bin/python scripts/run_detector.py [N]   (N = top-by-similarity, default 500)
"""

import asyncio
import json
import sys
import time
from collections import Counter

import aiohttp
import websockets

from pmarb.credentials import PolymarketUSCredentials
from pmarb.detection.spread import detect_pair
from pmarb.feeds._util import now_utc
from pmarb.feeds.auth import polymarket_us_headers
from pmarb.feeds.kalshi import _REST, normalize_orderbook
from pmarb.feeds.polymarket import _UA, _WS_PATH, _WS_URL, normalize_market_data

NO_STALENESS = 10**9  # disable staleness gate for a batch snapshot


async def fetch_kalshi_book(session, ticker, sem):
    async with sem:
        try:
            async with session.get(
                f"{_REST}/markets/{ticker}/orderbook",
                headers={"Accept": "application/json"},
            ) as r:
                ob = (await r.json()).get("orderbook_fp", {})
            return normalize_orderbook({"ticker": ticker}, ob, now_utc())
        except Exception:
            return None


async def collect_poly_books(creds, slugs, timeout=45):
    headers = {**polymarket_us_headers(creds, "GET", _WS_PATH), "User-Agent": _UA}
    books = {}
    async with websockets.connect(_WS_URL, additional_headers=headers) as ws:
        for i in range(0, len(slugs), 200):
            await ws.send(json.dumps({"subscribe": {
                "requestId": f"b{i}",
                "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                "marketSlugs": slugs[i:i + 200]}}))
        deadline = time.time() + timeout
        while len(books) < len(slugs) and time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=6)
            except asyncio.TimeoutError:
                break
            md = json.loads(raw).get("marketData")
            if md and md.get("marketSlug") and md["marketSlug"] not in books:
                books[md["marketSlug"]] = normalize_market_data(
                    {"slug": md["marketSlug"]}, md, now_utc())
    return books


def edge(a, b):
    """Best apparent top-of-book edge (before fees) over both directions."""
    best = None
    for yes_m, no_m in ((a, b), (b, a)):
        if yes_m.yes_ask is not None and no_m.no_ask is not None:
            e = 1.0 - yes_m.yes_ask - no_m.no_ask
            best = e if best is None else max(best, e)
    return best


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    creds = PolymarketUSCredentials.from_env()
    cands = json.load(open("matches.json"))
    cands.sort(key=lambda c: c["similarity_score"], reverse=True)
    cands = cands[:n]

    tickers = sorted({c["kalshi_id"].split(":", 1)[1] for c in cands})
    slugs = sorted({c["polymarket_id"].split(":", 1)[1] for c in cands})
    print(f"candidates: {len(cands)} | unique Kalshi {len(tickers)} | unique Poly {len(slugs)}")

    t = time.time()
    sem = asyncio.Semaphore(20)
    async with aiohttp.ClientSession() as s:
        k_books, p_books = await asyncio.gather(
            asyncio.gather(*(fetch_kalshi_book(s, tk, sem) for tk in tickers)),
            collect_poly_books(creds, slugs),
        )
    kalshi = {tk: b for tk, b in zip(tickers, k_books) if b is not None}
    print(f"fetched Kalshi books {len(kalshi)}/{len(tickers)}, "
          f"Poly books {len(p_books)}/{len(slugs)} in {time.time()-t:.0f}s\n")

    now = now_utc()
    edges = []
    viable = []
    both = 0
    for c in cands:
        k = kalshi.get(c["kalshi_id"].split(":", 1)[1])
        p = p_books.get(c["polymarket_id"].split(":", 1)[1])
        if not k or not p:
            continue
        both += 1
        e = edge(k, p)
        if e is not None:
            edges.append((e, c))
        for opp in detect_pair(k, p, now, max_staleness=NO_STALENESS):
            viable.append((opp, c))

    print(f"pairs with both live books: {both}")

    # apparent edge distribution
    buckets = Counter()
    for e, _ in edges:
        if e < 0: buckets["<0 (no edge)"] += 1
        elif e < 0.02: buckets["0-2%"] += 1
        elif e < 0.05: buckets["2-5%"] += 1
        elif e < 0.10: buckets["5-10%"] += 1
        else: buckets[">10% (suspect)"] += 1
    print("\napparent top-of-book edge distribution:")
    for k in ("<0 (no edge)", "0-2%", "2-5%", "5-10%", ">10% (suspect)"):
        print(f"  {k:16s} {buckets[k]}")

    print(f"\nVIABLE after fees+slippage: {len(viable)} (deduped below)")
    viable.sort(key=lambda x: x[0].fee_adjusted_spread, reverse=True)
    seen = set()
    for opp, c in viable:
        key = (c["kalshi_id"], c["polymarket_id"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  spread=${opp.fee_adjusted_spread:.4f} size={opp.size} "
              f"{opp.yes_platform}(yes)+{opp.no_platform}(no)")
        print(f"     K: {c['kalshi_question'][:55]}")
        print(f"     P: {c['polymarket_question'][:55]}")
        if len(seen) >= 12:
            break

    print("\nEXTREME apparent edges (>10%, likely mismatches):")
    for e, c in sorted(edges, key=lambda x: x[0], reverse=True)[:8]:
        if e > 0.10:
            print(f"  edge={e:.2%}  K:{c['kalshi_question'][:40]} | P:{c['polymarket_question'][:40]}")


if __name__ == "__main__":
    asyncio.run(main())
