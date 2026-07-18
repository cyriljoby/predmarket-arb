"""Live driver — stream both venues, detect + log opportunities in real time.

Wires everything together: loads the trusted matched pairs (structured +
futures), streams both books concurrently into a shared cache, and on every
update re-evaluates that market's matched partner with the REAL staleness gate
on (both legs now carry live timestamps). Any pair with a positive top-of-book
edge is written to `opportunities.jsonl` for the backtest.

Run:  .venv/bin/python -m pmarb.main [DURATION_SECONDS]
      (no duration -> runs until Ctrl+C)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from collections import defaultdict

import aiohttp

from pmarb.config import (
    MATCH_LOG_PATH,
    RECONNECT_BASE_SECONDS,
    RECONNECT_MAX_SECONDS,
)
from pmarb.credentials import KalshiCredentials, PolymarketUSCredentials
from pmarb.detection.spread import evaluate_pair
from pmarb.feeds._util import now_utc
from pmarb.feeds.kalshi import KalshiFeed
from pmarb.feeds.polymarket import PolymarketUSFeed
from pmarb.config import LATEST_LOG_PATH, LOG_HEARTBEAT_SECONDS
from pmarb.oplog import LatestOpportunityLog, OpportunityLogger

# Only stream the trustworthy tiers — lexical is noise (see multi-outcome guard).
_TRUSTED = {"structured", "futures"}


async def _fetch_with_retry(feed, attempts: int = 8):
    """Startup market discovery over REST — retry transient network failures
    (connection resets, DNS blips, gateway 5xx) so a blip at launch doesn't
    kill an unattended run."""
    backoff = RECONNECT_BASE_SECONDS
    for attempt in range(1, attempts + 1):
        try:
            return await feed.fetch_markets()
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            if attempt == attempts:
                raise
            print(f"  {feed.platform} fetch_markets failed "
                  f"({type(exc).__name__}: {exc}); retry in {backoff:.0f}s "
                  f"[{attempt}/{attempts}]")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)


async def run(duration: float | None) -> None:
    kcreds = KalshiCredentials.from_env()
    pcreds = PolymarketUSCredentials.from_env()

    matches = [
        m for m in json.load(open(MATCH_LOG_PATH))
        if m.get("match_method") in _TRUSTED
    ]
    # market id -> the matches it participates in (usually one)
    index: dict[str, list[dict]] = defaultdict(list)
    for m in matches:
        index[m["kalshi_id"]].append(m)
        index[m["polymarket_id"]].append(m)
    need_k = {m["kalshi_id"] for m in matches}
    need_p = {m["polymarket_id"] for m in matches}
    print(f"loaded {len(matches)} trusted pairs "
          f"({len(need_k)} Kalshi, {len(need_p)} Poly markets)")

    async with aiohttp.ClientSession() as session:
        kfeed = KalshiFeed(session, kcreds)
        pfeed = PolymarketUSFeed(session, pcreds)
        k_all, p_all = await asyncio.gather(
            _fetch_with_retry(kfeed), _fetch_with_retry(pfeed)
        )
        k_mkts = [m for m in k_all if m.id in need_k]
        p_mkts = [m for m in p_all if m.id in need_p]
        print(f"streaming {len(k_mkts)} Kalshi + {len(p_mkts)} Poly books "
              f"(duration={duration or 'until Ctrl+C'})\n")

        cache: dict[str, object] = {}
        # Two sinks: an append-only event log (time series -> backtest) and a
        # keyed latest-snapshot (one line per pair -> live "what's open now").
        event_log = OpportunityLogger()            # opportunities.jsonl
        latest_log = LatestOpportunityLog(LATEST_LOG_PATH, flush_interval=2.0)  # snapshot
        last_append: dict[tuple, float] = {}       # per-pair last append-log time
        stats = {"updates": 0, "windows": 0}

        async def consume(feed, mkts) -> None:
            # The feed's own reconnect loop handles network drops; this outer
            # loop is the last line of defense against everything else (a
            # malformed message, a normalize bug) — log it, back off, restart
            # the stream. An unattended multi-day run must not die silently.
            backoff = RECONNECT_BASE_SECONDS
            while True:
                try:
                    await _consume_stream(feed, mkts)
                    return  # stream ended cleanly (finite market list closed)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    print(f"  {feed.platform} consumer crashed; "
                          f"restarting in {backoff:.0f}s")
                    traceback.print_exc()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

        async def _consume_stream(feed, mkts) -> None:
            async for mk in feed.stream_books(mkts):
                cache[mk.id] = mk
                stats["updates"] += 1
                now = now_utc()
                for match in index.get(mk.id, ()):
                    partner_id = (
                        match["polymarket_id"] if mk.platform == "kalshi"
                        else match["kalshi_id"]
                    )
                    partner = cache.get(partner_id)
                    if partner is None:
                        continue  # partner not seen yet — wait for its first book
                    k, p = (mk, partner) if mk.platform == "kalshi" else (partner, mk)
                    # require_edge=False: record the full spread distribution, not
                    # just positive windows (None only if stale / one-sided book).
                    ev = evaluate_pair(k, p, now, require_edge=False)
                    if ev is None:
                        continue
                    # Keyed snapshot: every pair, always (bounded — one line/pair).
                    latest_log.log(ev, match)
                    # Append time series, kept bounded:
                    #  - a truly VIABLE window (fillable size > 0) is always logged
                    #    (real arb — rare, never miss one);
                    #  - otherwise only LIVE GAME markets (structured tier) get a
                    #    throttled heartbeat sample. Static futures/outrights sit at
                    #    fake positive edges on illiquid books and would firehose,
                    #    so they're logged only when actually viable.
                    is_viable = ev.estimated_fillable_size > 0
                    is_structured = match.get("match_method") == "structured"
                    key = (match["kalshi_id"], match["polymarket_id"])
                    tmono = time.monotonic()
                    last_t, last_edge = last_append.get(key, (0.0, None))
                    edge = round(ev.raw_spread_top_of_book, 4)
                    # Sample a live game (structured) or any viable pair, but only
                    # when the edge actually CHANGED and the heartbeat elapsed —
                    # quiet games log once then go silent; live games track moves.
                    if ((is_structured or is_viable)
                            and edge != last_edge
                            and tmono - last_t >= LOG_HEARTBEAT_SECONDS):
                        event_log.log(ev, match)
                        last_append[key] = (tmono, edge)
                        if is_viable:
                            stats["windows"] += 1

        async def report() -> None:
            t0 = time.time()
            while True:
                await asyncio.sleep(10)
                print(f"  [{time.time() - t0:4.0f}s] updates={stats['updates']:>7} "
                      f"books={len(cache):>5} samples={event_log.count} "
                      f"windows={stats['windows']} tracked={latest_log.count}")

        tasks = [
            asyncio.create_task(consume(kfeed, k_mkts)),
            asyncio.create_task(consume(pfeed, p_mkts)),
            asyncio.create_task(report()),
        ]
        try:
            if duration:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=duration)
            else:
                await asyncio.gather(*tasks)
        except (asyncio.TimeoutError, KeyboardInterrupt):
            pass
        finally:
            for t in tasks:
                t.cancel()
            event_log.close()
            latest_log.close()
            print(f"\n{event_log.count} samples ({stats['windows']} positive-edge "
                  f"windows) over {stats['updates']} updates "
                  f"-> {event_log._path} (append, for backtest)\n"
                  f"{latest_log.count} pairs tracked "
                  f"-> {latest_log._path} (latest snapshot)")


def main() -> None:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(run(duration))


if __name__ == "__main__":
    main()
