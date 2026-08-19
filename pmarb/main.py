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
    EDGE_CHANGE_THROTTLE_SECONDS,
    HEARTBEAT_SECONDS,
    LATEST_LOG_PATH,
    MATCH_LOG_PATH,
    RECONNECT_BASE_SECONDS,
    RECONNECT_MAX_SECONDS,
)
from pmarb.credentials import KalshiCredentials, PolymarketUSCredentials
from pmarb.db import connect, dsn
from pmarb.db.schema import ensure_partitions
from pmarb.db.sync import pair_ids, upsert_markets
from pmarb.db.writer import ObservationWriter, to_row
from pmarb.detection.spread import evaluate_pair
from pmarb.feeds._util import now_utc
from pmarb.feeds.kalshi import KalshiFeed
from pmarb.feeds.polymarket import PolymarketUSFeed
from pmarb.oplog import LatestOpportunityLog, OpportunityLogger

# Only stream the trustworthy tiers — lexical is noise (see multi-outcome guard).
_TRUSTED = {"structured", "futures"}


# Why a row exists. Persisted on every observation, because "no row" and "a row
# we chose not to write" are different facts and the difference is what made
# Phase 1's futures numbers floors rather than measurements.
HEARTBEAT, EDGE_CHANGE, VIABLE, WINDOW_CLOSE = 0, 1, 2, 3


def sample_reason(
    *, is_viable: bool, was_viable: bool, is_structured: bool,
    edge: float, last_edge: float | None, seconds_since_last: float,
    throttle: float = EDGE_CHANGE_THROTTLE_SECONDS,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> int | None:
    """Why this evaluation should be recorded, or None to skip it.

    A uniform throttle destroys the one thing the log exists to measure. A
    window opens and closes ON a book update, so every evaluation while viable
    is recorded — that puts duration resolution at the update stream rather than
    at an arbitrary timer. Phase 1 throttled the viable case too, so any window
    shorter than the throttle produced a single row and read as 0s; 86% of them
    did.

      VIABLE       -> always, unthrottled.
      WINDOW_CLOSE -> the first NON-viable evaluation after a viable one, and
                      the only thing that pins the window's end to better than
                      one heartbeat.
      EDGE_CHANGE  -> throttled, and only live games, whose edge actually moves.
                      Static outrights sit at fake positive edges on illiquid
                      books and would firehose.
      HEARTBEAT    -> nothing happened, but record it anyway every `heartbeat`
                      seconds. This is the denominator: an absence of rows
                      cannot distinguish "watched and never viable" from "never
                      watched", and that ambiguity is what made Phase 1's rates
                      floors instead of measurements. Last in the chain, so it
                      only ever fires when no more specific reason applies.
    """
    if is_viable:
        return VIABLE
    if was_viable:
        return WINDOW_CLOSE
    if (is_structured and edge != last_edge
            and seconds_since_last >= throttle):
        return EDGE_CHANGE
    if seconds_since_last >= heartbeat:
        return HEARTBEAT
    return None


async def _fetch_with_retry(feed, attempts: int = 8):
    """Startup market discovery over REST — retry transient network failures
    (connection resets, DNS blips, gateway 5xx) so a blip at launch doesn't
    kill an unattended run."""
    backoff = RECONNECT_BASE_SECONDS
    for attempt in range(1, attempts + 1):
        try:
            return await feed.fetch_markets()
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
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
        latest_log = LatestOpportunityLog(  # keyed snapshot
            LATEST_LOG_PATH, flush_interval=2.0
        )
        # DUAL-WRITE. Postgres is the destination; the JSONL sinks stay until the
        # database is trusted, so a writer bug costs debugging time rather than
        # a week of collection. Database problems must never stop collection, so
        # every failure here degrades to file-only with a warning.
        writer: ObservationWriter | None = None
        pair_id: dict[tuple[str, str], int] = {}
        try:
            with connect() as conn:
                ensure_partitions(conn)
                upsert_markets(conn, k_mkts + p_mkts)
                conn.commit()
                pair_id = pair_ids(conn)
            writer = ObservationWriter(dsn())
            await writer.start()
            print(f"dual-writing to Postgres ({len(pair_id)} known pairs)")
        except Exception as exc:
            print(f"  WARNING Postgres unavailable ({type(exc).__name__}: {exc}); "
                  f"continuing with file sinks only")
        # pair -> (last append time, last edge, was viable last evaluation)
        last_append: dict[tuple, tuple] = {}
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
                    # Append time series. The sampling rate is ASYMMETRIC on
                    # purpose, because a uniform throttle destroys the one thing
                    # this log exists to measure:
                    #   VIABLE      -> every evaluation, unthrottled. A window
                    #                  opens and closes ON a book update, so
                    #                  sampling each one measures its duration
                    #                  to the resolution of the update stream.
                    #   CLOSE       -> the first non-viable evaluation after a
                    #                  viable one, logged once. The only row
                    #                  written BECAUSE a pair is not viable, and
                    #                  the only thing that pins closed_at.
                    #   everything  -> throttled, and only live games, whose
                    #     else         edge actually moves. Static outrights sit
                    #                  at fake positive edges on illiquid books
                    #                  and would firehose.
                    # Phase 1 throttled the viable case too, so any window
                    # shorter than the heartbeat produced one row and read as
                    # 0s duration — 86% of them did.
                    is_viable = ev.estimated_fillable_size > 0
                    is_structured = match.get("match_method") == "structured"
                    key = (match["kalshi_id"], match["polymarket_id"])
                    tmono = time.monotonic()
                    last_t, last_edge, was_viable = last_append.get(
                        key, (0.0, None, False)
                    )
                    edge = round(ev.raw_spread_top_of_book, 4)

                    reason = sample_reason(
                        is_viable=is_viable,
                        was_viable=was_viable,
                        is_structured=is_structured,
                        edge=edge,
                        last_edge=last_edge,
                        seconds_since_last=tmono - last_t,
                    )

                    if reason is not None:
                        # Heartbeats go to Postgres only. They exist to make the
                        # denominator countable, and the JSONL sinks are the
                        # Phase 1 opportunity log — pouring ~375k rows/day of
                        # "nothing happened" into them would change what that
                        # file is without making any window easier to see.
                        if reason != HEARTBEAT:
                            event_log.log(ev, match)
                        pid = pair_id.get(key)
                        if writer is not None and pid is not None:
                            writer.submit(to_row(
                                ev, pid, reason, now,
                                k.resolution_date, p.resolution_date))
                        last_append[key] = (tmono, edge, is_viable)
                        if is_viable and not was_viable:
                            stats["windows"] += 1   # count WINDOWS, not samples
                    else:
                        last_append[key] = (last_t, last_edge, is_viable)

        async def report() -> None:
            t0 = time.time()
            while True:
                await asyncio.sleep(10)
                db = ""
                if writer is not None:
                    w = writer.stats
                    # dropped/failed are surfaced every tick: a silent drop means
                    # the database fell behind and observations were lost.
                    db = (f" db={w.written}"
                          + (f" DROPPED={w.dropped}" if w.dropped else "")
                          + (f" FAILED={w.failed}" if w.failed else ""))
                print(f"  [{time.time() - t0:4.0f}s] updates={stats['updates']:>7} "
                      f"books={len(cache):>5} samples={event_log.count} "
                      f"windows={stats['windows']} tracked={latest_log.count}{db}")

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
        except (TimeoutError, KeyboardInterrupt):
            pass
        finally:
            for t in tasks:
                t.cancel()
            event_log.close()
            latest_log.close()
            if writer is not None:
                await writer.stop()
                print(f"  db writer: {writer.stats}")
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
