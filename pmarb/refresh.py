"""Daily in-process refresh of the tracked match set.

WHY THIS IS NOT A RESTART. 78% of tracked pairs are tied to a single game
(props 3,042 + lines 2,134 + structured 626 of 7,393), so the match set decays
daily, while discovery ran exactly once at startup: `books` sat at 14,780 for
fourteen days, which means settled games were still subscribed. That is the
mechanism behind the stale-book artifacts — NASCAR quoted at 83c, F1
constructors at 81c, on books nobody had traded since the event finished.

A restart would refresh the set, and would also destroy `last_append`
(pair -> (last_t, last_edge, was_viable, yes_venue)). `was_viable` is the ONLY
thing that triggers a close-marker row, and that row is the only thing that pins
a window's `closed_at`. So a restart leaves every open window unclosed and makes
the next viable sample look like a NEW window: one window becomes two, both with
wrong durations, biasing the survival curve short. Preserving that state in
process is the entire point of this module — and the same reason the latency
histogram and the run counters are never reset here.

WHAT A CYCLE DOES, in this order, because the order is load-bearing:
  1. re-fetch both catalogs and reload matches.json (nothing mutated yet, so a
     network failure here changes nothing);
  2. diff the pair set (pure);
  3. sync the database — partitions, markets, match_pairs, then reread
     `pair_id`. This comes BEFORE any subscription goes live: an observation for
     a pair with no `match_pair` row is silently dropped by the writer;
  4. write a close marker for every dropped pair whose window was open;
  5. prune and extend the in-memory state in one pass, so no exception can leave
     `index` and `pair_id` describing different match sets;
  6. mutate the live subscriptions (see each feed's `resync`).
"""

from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aiohttp

from pmarb.config import (
    MATCH_LOG_PATH,
    MATCH_REFRESH_HOUR_UTC,
    RECONNECT_BASE_SECONDS,
    RECONNECT_MAX_SECONDS,
)
from pmarb.db import connect as _connect
from pmarb.db.schema import ensure_partitions
from pmarb.db.sync import pair_ids, upsert_markets, upsert_match_pairs
from pmarb.db.writer import close_marker_row
from pmarb.feeds._util import now_utc
from pmarb.models.sample import UNSUBSCRIBED

# Only stream the trustworthy tiers — lexical is noise (see multi-outcome guard).
TRUSTED_MATCH_METHODS = {"structured", "futures", "line", "prop"}

REVIEW_LOG_PATH = "reviews.json"


def pair_key(match: dict) -> tuple[str, str]:
    return (match["kalshi_id"], match["polymarket_id"])


async def fetch_with_retry(feed, attempts: int = 8):
    """Market discovery over REST — retry transient network failures
    (connection resets, DNS blips, gateway 5xx) so a blip doesn't kill an
    unattended run, at launch or on a daily refresh."""
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


def load_trusted_matches(path: str = MATCH_LOG_PATH) -> tuple[list[dict], list[dict]]:
    """(every match, the trusted subset) from matches.json.

    BOTH are returned because they answer different questions. Streaming uses the
    trusted subset. `upsert_match_pairs` needs the FULL list, because it retracts
    every pair it is not shown — handing it only the trusted tiers would retract
    the entire lexical catalog, which the backtest reads as "the matcher withdrew
    them" rather than "we don't stream them".
    """
    matches = json.load(open(path))
    trusted = [m for m in matches
               if m.get("match_method") in TRUSTED_MATCH_METHODS]
    return matches, trusted


def load_reviews(path: str = REVIEW_LOG_PATH) -> list[dict]:
    """Human verdicts, or none. Passed to `upsert_match_pairs` because it writes
    review fields VERBATIM — refreshing pairs without them would blank every
    label, which is how 21 withdrawn verdicts silently came back once already."""
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return []


@dataclass(frozen=True, slots=True)
class PairDiff:
    """What changed, in pairs and in markets."""

    added: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)

    @property
    def live(self) -> list[dict]:
        """Every pair to be tracked after this refresh."""
        return self.kept + self.added

    def market_ids(self) -> set[str]:
        return {m[k] for m in self.live for k in ("kalshi_id", "polymarket_id")}


def pair_diff(tracked: dict[tuple[str, str], dict], trusted: list[dict],
              listed: set[str]) -> PairDiff:
    """Diff the tracked pair set against a fresh catalog + match set.

    A pair is DROPPED when either leg is no longer listed on its venue (the game
    settled, the market delisted) or when it left matches.json entirely (the
    matcher withdrew it). A pair is ADDED when it is trusted, both legs are
    listed, and it is not already tracked.

    `listed` is the union of both venues' currently-listed market ids: one set
    rather than two, because a pair's legs are already venue-tagged in their ids
    and a per-venue split only invites a mix-up.

    Pairs that are in matches.json but whose legs are NOT listed are not tracked
    either — they are exactly what startup silently ignored, and keeping them in
    `index` grows memory across refreshes for markets that will never tick.
    """
    fresh = {pair_key(m): m for m in trusted
             if m["kalshi_id"] in listed and m["polymarket_id"] in listed}
    added = [m for key, m in fresh.items() if key not in tracked]
    # Fresh metadata wins for a surviving pair: hazards and review state are
    # amended between refreshes, and the log records what was believed when the
    # pair was streamed.
    kept = [m for key, m in fresh.items() if key in tracked]
    dropped = [m for key, m in tracked.items() if key not in fresh]
    return PairDiff(added=added, dropped=dropped, kept=kept)


def seconds_until_hour(now: datetime, hour: int) -> float:
    """Seconds from `now` to the next occurrence of `hour`:00 UTC.

    Pure, so the schedule is testable without waiting on a wall clock: the
    refresh loop asks this for its next delay and sleeps exactly that long.
    Exactly on the hour returns a full day rather than 0, so a cycle that starts
    at 11:00:00 cannot immediately re-fire.
    """
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class MatchSetRefresher:
    """Owns one refresh cycle over the collector's live state.

    Every piece of collector state is passed in and mutated IN PLACE — `index`,
    `cache`, `last_append`, `pair_id`, and the two market lists the consumers
    were started with. In place because the hot loop closed over those objects:
    rebinding a name here would leave the detector reading the old dict, which is
    the kind of bug that shows up as a slow drift in what gets logged rather than
    as a failure.
    """

    def __init__(
        self, *, kfeed, pfeed,
        index: dict[str, list[dict]],
        cache: dict,
        last_append: dict,
        pair_id: dict[tuple[str, str], int],
        k_markets: list,
        p_markets: list,
        writer=None,
        latest_log=None,
        match_log_path: str = MATCH_LOG_PATH,
        review_log_path: str = REVIEW_LOG_PATH,
        fetch=fetch_with_retry,
        connect=_connect,
        clock=now_utc,
    ):
        self._kfeed, self._pfeed = kfeed, pfeed
        self._index = index
        self._cache = cache
        self._last_append = last_append
        self._pair_id = pair_id
        self._k_markets, self._p_markets = k_markets, p_markets
        self._writer = writer
        self._latest_log = latest_log
        self._match_log_path = match_log_path
        self._review_log_path = review_log_path
        self._fetch = fetch
        self._connect = connect
        self._clock = clock
        self.cycles = 0

    # -- state ------------------------------------------------------------- #
    def tracked(self) -> dict[tuple[str, str], dict]:
        """The currently tracked pairs, derived from `index`.

        Derived rather than kept alongside it, so there is exactly one source of
        truth for what is being watched and no way for the two to disagree.
        """
        return {pair_key(m): m for ms in self._index.values() for m in ms}

    # -- one cycle --------------------------------------------------------- #
    async def refresh_once(self) -> dict:
        """Run one full refresh. Returns a summary dict for logging."""
        self.cycles += 1
        # json.load of a multi-megabyte match set is not free, and the hot path
        # is a WebSocket reader: off-thread so a refresh never adds latency to a
        # book update (a stalled read on Kalshi costs a full resnapshot).
        all_matches, trusted = await asyncio.to_thread(
            load_trusted_matches, self._match_log_path)
        k_all, p_all = await asyncio.gather(
            self._fetch(self._kfeed), self._fetch(self._pfeed))
        by_id = {m.id: m for m in k_all + p_all}

        diff = pair_diff(self.tracked(), trusted, set(by_id))
        live_ids = diff.market_ids()
        k_mkts = [m for m in k_all if m.id in live_ids]
        p_mkts = [m for m in p_all if m.id in live_ids]
        summary = {
            "added": len(diff.added), "dropped": len(diff.dropped),
            "tracked": len(diff.live), "closed": 0,
            "kalshi_books": len(k_mkts), "poly_books": len(p_mkts),
        }

        # 3. Database FIRST. A pair whose `match_pair` row does not exist yet has
        # its observations silently dropped by the writer (`pair_id.get` misses),
        # so this has to land before the new subscriptions do. A database problem
        # must never stop collection, so a failure here degrades to file-only for
        # the new pairs and is reported rather than raised.
        try:
            new_pair_id = await asyncio.to_thread(
                self._sync_db, k_mkts + p_mkts, all_matches)
        except Exception as exc:
            new_pair_id = None
            print(f"  WARNING refresh could not sync Postgres "
                  f"({type(exc).__name__}: {exc}); new pairs will write to the "
                  f"file sinks only until the next refresh")

        # 4+5. Close markers and the in-memory rewrite, together and last: from
        # here on nothing awaits, so no exception can interleave and leave
        # `index` and `pair_id` describing different match sets.
        summary["closed"] = self._close_open_windows(diff.dropped)
        self._apply(diff, by_id, k_mkts, p_mkts, new_pair_id)

        # 6. The venues. Each is independent: one refusing a mutation must not
        # leave the other on a stale list.
        for feed, mkts in ((self._kfeed, k_mkts), (self._pfeed, p_mkts)):
            try:
                summary[feed.platform] = await feed.resync(mkts)
            except Exception as exc:
                # The corrected list is already in the feed's own state and in
                # the consumer's market list, so the worst case is that the next
                # reconnect applies it. Never fatal.
                summary[feed.platform] = {"applied": "failed",
                                          "error": f"{type(exc).__name__}: {exc}"}
                print(f"  WARNING {feed.platform} resync raised "
                      f"({type(exc).__name__}: {exc})")
                traceback.print_exc()
        return summary

    def _sync_db(self, markets, all_matches) -> dict:
        """Partitions, markets, pairs, then the fresh pair_id mapping.

        Synchronous psycopg, called via to_thread. `ensure_partitions` is here
        and not only at startup because a month boundary crossed mid-run makes
        every insert fail outright — Postgres does not create partitions on
        demand.
        """
        reviews = load_reviews(self._review_log_path)
        with self._connect() as conn:
            ensure_partitions(conn)
            upsert_markets(conn, markets)
            res = upsert_match_pairs(conn, all_matches, reviews)
            conn.commit()
            mapping = pair_ids(conn)
        print(f"  refresh db: pairs upserted {res['upserted']}, retracted "
              f"{res['retracted']}, skipped {res['skipped']} (leg delisted)")
        return mapping

    def _close_open_windows(self, dropped: list[dict]) -> int:
        """Write a close marker for every dropped pair that was still viable.

        Without this a window open at removal time never closes: `was_viable` is
        what triggers a close marker, and pruning the pair takes that flag with
        it. The marker's reason is UNSUBSCRIBED, not WINDOW_CLOSE, because the
        two are opposite facts — one says an evaluation found the edge gone, this
        one says we stopped looking. The survival curve counts censored windows
        separately, and reporting our own unsubscribe as an observed close would
        manufacture decay that never happened.
        """
        if self._writer is None:
            return 0
        now = self._clock()
        closed = 0
        for match in dropped:
            key = pair_key(match)
            state = self._last_append.get(key)
            pid = self._pair_id.get(key)
            if not state or not state[2] or pid is None:
                continue          # never viable, or no row to attach it to
            self._writer.submit(close_marker_row(pid, UNSUBSCRIBED, now,
                                                 state[3] or "kalshi"))
            closed += 1
        return closed

    def _apply(self, diff: PairDiff, by_id: dict, k_mkts, p_mkts,
               new_pair_id: dict | None) -> None:
        """Rewrite the in-memory state to the new match set.

        Purely synchronous and total: `index` is rebuilt from the live pairs, and
        every other structure keyed by pair or by market is pruned to match, so
        memory does not grow across refreshes. Deliberately NOT touched: the
        latency histogram and the run counters, which exist to describe the whole
        run and would be silently reset by a refresh that "cleaned up".
        """
        live_pairs = {pair_key(m) for m in diff.live}
        live_ids = diff.market_ids()

        self._index.clear()
        for match in diff.live:
            # setdefault rather than a defaultdict's [] so this works on a plain
            # dict too: `index` belongs to the caller, not to this module.
            self._index.setdefault(match["kalshi_id"], []).append(match)
            self._index.setdefault(match["polymarket_id"], []).append(match)

        for key in [k for k in self._last_append if k not in live_pairs]:
            del self._last_append[key]
        for mid in [m for m in self._cache if m not in live_ids]:
            del self._cache[mid]
        if self._latest_log is not None:
            # The keyed snapshot is one line per pair, so a dropped pair would
            # otherwise sit in "what's open now" forever — a settled game shown
            # as a currently-open candidate is the artifact in miniature.
            self._latest_log.prune(live_pairs)

        if new_pair_id is not None:
            self._pair_id.clear()
            self._pair_id.update(new_pair_id)

        # The consumers were started with these lists and restart `stream_books`
        # from them after a crash, so they have to be corrected in place — a
        # consumer that crashes tomorrow must not resubscribe yesterday's slate.
        self._k_markets[:] = k_mkts
        self._p_markets[:] = p_mkts

    # -- the schedule ------------------------------------------------------- #
    async def run(self, *, hour: int = MATCH_REFRESH_HOUR_UTC,
                  interval: float | None = None,
                  sleep=asyncio.sleep, cycles: int | None = None) -> None:
        """Refresh daily at `hour`:00 UTC, forever.

        The schedule is injectable rather than a hardcoded sleep-until-wall-clock
        so it can be driven in tests: `interval` overrides the daily schedule
        with a fixed delay, `sleep` and the constructor's `clock` can be faked,
        and `cycles` bounds the loop. A schedule that can only be exercised by
        waiting until 11:00 UTC is a schedule nobody exercises.

        A cycle that raises is logged and the loop continues. A refresh failing
        costs a day of stale books; the loop dying costs the run, because nothing
        would ever refresh again and — worse — it would look fine.
        """
        done = 0
        while cycles is None or done < cycles:
            delay = (interval if interval is not None
                     else seconds_until_hour(self._clock(), hour))
            await sleep(delay)
            try:
                summary = await self.refresh_once()
                print(f"  refresh: +{summary['added']} pairs, "
                      f"-{summary['dropped']} ({summary['closed']} open windows "
                      f"closed), now {summary['tracked']} pairs / "
                      f"{summary['kalshi_books']} Kalshi + "
                      f"{summary['poly_books']} Poly books; "
                      f"kalshi={summary.get('kalshi', {}).get('applied')} "
                      f"poly={summary.get('polymarket_us', {}).get('applied')}")
            except asyncio.CancelledError:
                raise
            except Exception:
                print("  WARNING match refresh cycle failed; the tracked set is "
                      "unchanged and the next cycle will retry")
                traceback.print_exc()
            done += 1
