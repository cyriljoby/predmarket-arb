"""
spike.py — day-one connectivity spike (THROWAWAY, not project code).

Goal: prove we can pull live order books from BOTH platforms and SEE their raw
wire shapes, with zero credentials. This is the learning experiment that de-risks
the real feeds in feeds/kalshi.py and feeds/polymarket.py.

  - Polymarket: public WebSocket (wss://ws-subscriptions-clob.polymarket.com/ws/market)
  - Kalshi:     public REST order book (its WebSocket needs RSA-signed auth, deferred
                until we build the real feed; REST market data is public)

No normalization, no Market schema, no logging. Just raw books to stdout so a human
can read the actual field names. Findings learned while writing this:
  * Kalshi's flat /markets list is swamped by auto-generated multivariate "KXMVE..."
    provisional markets with null quotes. Real markets come from the EVENTS endpoint
    with_nested_markets=true.
  * Kalshi order book = {"orderbook_fp": {"yes_dollars": [[price,size],...],
    "no_dollars": [[price,size],...]}} with prices/sizes as STRINGS. Only BID ladders
    are given; a YES ask is a NO bid at (1 - price).
  * Polymarket clobTokenIds = ["<yes_token>", "<no_token>"]; token id is the WS asset id.

Run:  .venv/bin/python spike.py
"""

import asyncio
import json

import aiohttp
import websockets

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com/markets"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

POLY_MAX_MESSAGES = 6  # stop after this many WS messages so the spike exits


# --------------------------------------------------------------------------- #
# Kalshi: public REST order book                                              #
# --------------------------------------------------------------------------- #
async def kalshi_spike(session: aiohttp.ClientSession) -> None:
    print("\n" + "=" * 70)
    print("KALSHI — discovering a liquid market via the events endpoint")
    print("=" * 70)

    async with session.get(
        f"{KALSHI_BASE}/events",
        params={"status": "open", "with_nested_markets": "true", "limit": "200"},
    ) as resp:
        data = await resp.json()

    # Collect real (non-MVE) markets that have a two-sided quote.
    candidates = []
    for event in data.get("events", []):
        for m in event.get("markets", []):
            if m["ticker"].startswith("KXMVE"):
                continue
            if m.get("yes_bid_dollars") is not None and m.get("yes_ask_dollars") is not None:
                candidates.append((m["ticker"], event.get("title", "")))

    print(f"non-MVE markets with a two-sided quote: {len(candidates)}")

    # Walk candidates until one returns a non-empty order book.
    for ticker, title in candidates[:15]:
        async with session.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook") as resp:
            ob = (await resp.json()).get("orderbook_fp", {})
        yes = ob.get("yes_dollars") or []
        no = ob.get("no_dollars") or []
        if yes or no:
            print(f"\nticker : {ticker}")
            print(f"title  : {title}")
            print(f"\nraw orderbook_fp (prices & sizes are STRINGS, only BID ladders):")
            print(f"  yes_dollars (YES bids) — top 5: {yes[:5]}")
            print(f"  no_dollars  (NO  bids) — top 5: {no[:5]}")
            print("\n  note: a YES ask = a NO bid at (1 - price). Derivation happens")
            print("        in the normalizer later, not here.")
            return

    print("no candidate returned a populated order book (try re-running)")


# --------------------------------------------------------------------------- #
# Polymarket: public WebSocket                                                #
# --------------------------------------------------------------------------- #
async def poly_spike(session: aiohttp.ClientSession) -> None:
    print("\n" + "=" * 70)
    print("POLYMARKET — discovering a liquid market via Gamma REST")
    print("=" * 70)

    async with session.get(
        POLY_GAMMA,
        params={
            "active": "true",
            "closed": "false",
            "limit": "20",
            "order": "volumeNum",
            "ascending": "false",
        },
    ) as resp:
        markets = await resp.json()

    # Pick the highest-volume market that actually has a CLOB order book.
    market = next(
        (m for m in markets if m.get("enableOrderBook") and m.get("clobTokenIds")),
        None,
    )
    if market is None:
        print("no market with an enabled order book found")
        return

    token_ids = json.loads(market["clobTokenIds"])  # JSON string -> list
    outcomes = json.loads(market.get("outcomes", '["Yes","No"]'))
    yes_token = token_ids[0]

    print(f"question : {market['question']}")
    print(f"outcomes : {outcomes}")
    print(f"yes_token: {yes_token}")
    print(f"\nconnecting to {POLY_WS} and subscribing to the YES token ...")

    async with websockets.connect(POLY_WS) as ws:
        await ws.send(json.dumps({"assets_ids": [yes_token], "type": "market"}))
        for i in range(POLY_MAX_MESSAGES):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                # A quiet socket just means no book changes right now (thin market),
                # not a failure — we already proved the connection and got a snapshot.
                print(f"\n[{i}] no further updates in 10s — feed is just quiet, stopping")
                break
            msg = json.loads(raw)
            # Messages arrive as a list of events or a single dict.
            events = msg if isinstance(msg, list) else [msg]
            for ev in events:
                etype = ev.get("event_type", "?")
                if etype == "book":
                    bids = ev.get("bids", [])[:3]
                    asks = ev.get("asks", [])[:3]
                    print(f"\n[{i}] event_type=book  top bids={bids}  top asks={asks}")
                else:
                    print(f"\n[{i}] event_type={etype}  {json.dumps(ev)[:160]}")


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        # Kalshi first (one-shot REST), then stream Polymarket WS.
        await kalshi_spike(session)
        await poly_spike(session)
    print("\n" + "=" * 70)
    print("spike complete — both books printed, zero credentials used")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
