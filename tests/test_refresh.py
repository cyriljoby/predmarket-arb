"""The daily match-set refresh: what it drops, what it closes, what it forgets.

Three properties are load-bearing and none of them is visible from a passing
collector:

  * the DIFF, because a pair whose game settled must leave the subscription or
    the collector keeps quoting a post-event book (NASCAR at 83c);
  * the CLOSE MARKER on a dropped pair, because `was_viable` is the only thing
    that pins a window's end, and it dies with the pair — and it must be
    DISTINGUISHABLE from end-of-observation censoring, or the survival curve
    reads our own unsubscribe as market decay;
  * the PRUNE leaving no residue, because a refresh that only ever adds turns a
    multi-week run into a memory leak.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from pmarb import refresh as rmod
from pmarb.main import WINDOW_CLOSE
from pmarb.models.sample import UNSUBSCRIBED
from pmarb.refresh import MatchSetRefresher, pair_diff, seconds_until_hour

NOW = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)


def _match(k, p, method="structured"):
    return {"kalshi_id": f"kalshi:{k}", "polymarket_id": f"polymarket_us:{p}",
            "match_method": method, "similarity_score": 1.0}


class _Mkt:
    """A market as the refresh sees it: an id and a venue, nothing more."""

    def __init__(self, mid):
        self.id = mid
        self.platform = "kalshi" if mid.startswith("kalshi:") else "polymarket_us"


class _Feed:
    def __init__(self, platform, markets):
        self.platform = platform
        self.markets = markets
        self.resynced = None

    async def resync(self, markets):
        self.resynced = [m.id for m in markets]
        return {"applied": "live"}


class _Writer:
    def __init__(self):
        self.rows = []

    def submit(self, row):
        self.rows.append(row)


def _refresher(*, k_ids, p_ids, tracked, last_append=None,
               cache=None, pair_id=None, latest_log=None, writer=None,
               fetch=None):
    """A refresher wired to fakes, with the collector's structures pre-seeded."""
    index = {}
    for m in tracked:
        index.setdefault(m["kalshi_id"], []).append(m)
        index.setdefault(m["polymarket_id"], []).append(m)
    kfeed = _Feed("kalshi", [_Mkt(i) for i in k_ids])
    pfeed = _Feed("polymarket_us", [_Mkt(i) for i in p_ids])

    async def default_fetch(feed):
        return feed.markets

    ref = MatchSetRefresher(
        kfeed=kfeed, pfeed=pfeed, index=index, cache=cache if cache is not None else {},
        last_append=last_append if last_append is not None else {},
        pair_id=pair_id if pair_id is not None else {},
        k_markets=[], p_markets=[], writer=writer, latest_log=latest_log,
        fetch=fetch or default_fetch, clock=lambda: NOW,
    )
    return ref


@pytest.fixture
def stub_db(monkeypatch):
    """No database: `_sync_db` is the one step that needs one, and its failure
    path is already the documented degradation (file sinks only)."""
    monkeypatch.setattr(MatchSetRefresher, "_sync_db",
                        lambda self, markets, all_matches: {})


@pytest.fixture
def match_file(monkeypatch):
    """Serve matches.json from memory, in the loader's real shape."""
    def _set(matches):
        monkeypatch.setattr(rmod, "load_trusted_matches",
                            lambda path: (matches, [
                                m for m in matches
                                if m["match_method"] in rmod.TRUSTED_MATCH_METHODS]))
    return _set


class TestPairDiff:
    def test_a_pair_whose_leg_delisted_is_dropped(self):
        # The settled game. Both venues stop listing it; the collector must stop
        # subscribing it, which is the entire point of the refresh.
        m = _match("A", "a")
        diff = pair_diff({("kalshi:A", "polymarket_us:a"): m}, [m],
                         listed={"polymarket_us:a"})
        assert [d["kalshi_id"] for d in diff.dropped] == ["kalshi:A"]
        assert diff.added == [] and diff.kept == []

    def test_a_pair_the_matcher_withdrew_is_dropped(self):
        m = _match("A", "a")
        diff = pair_diff({("kalshi:A", "polymarket_us:a"): m}, [],
                         listed={"kalshi:A", "polymarket_us:a"})
        assert len(diff.dropped) == 1

    def test_a_newly_listed_pair_is_added(self):
        old, new = _match("A", "a"), _match("B", "b")
        diff = pair_diff({("kalshi:A", "polymarket_us:a"): old}, [old, new],
                         listed={"kalshi:A", "polymarket_us:a",
                                 "kalshi:B", "polymarket_us:b"})
        assert [a["kalshi_id"] for a in diff.added] == ["kalshi:B"]
        assert [k["kalshi_id"] for k in diff.kept] == ["kalshi:A"]
        assert len(diff.live) == 2

    def test_the_loader_keeps_lexical_out_of_the_streamed_set_but_not_the_db(
            self, tmp_path):
        # Two different questions. Streaming lexical pairs is noise (the
        # multi-outcome phantoms), but `upsert_match_pairs` retracts every pair
        # it is NOT shown — so handing it only the trusted tiers would record the
        # whole lexical catalog as withdrawn by the matcher.
        import json
        path = tmp_path / "matches.json"
        path.write_text(json.dumps([_match("A", "a"),
                                    _match("B", "b", method="lexical")]))
        all_matches, trusted = rmod.load_trusted_matches(str(path))
        assert len(all_matches) == 2
        assert [m["kalshi_id"] for m in trusted] == ["kalshi:A"]

    def test_an_unlisted_new_pair_is_not_tracked(self):
        # matches.json outlives the catalog, so most of it references markets
        # that already expired. Tracking them would grow `index` forever.
        m = _match("B", "b")
        diff = pair_diff({}, [m], listed={"kalshi:B"})
        assert diff.added == [] and diff.live == []


class TestCloseMarkerOnDrop:
    def test_an_open_window_gets_a_marker_with_its_own_reason(
            self, stub_db, match_file):
        # Without this row the window never closes, because `was_viable` is what
        # triggers a close marker and pruning the pair takes it away.
        match_file([])
        w = _Writer()
        key = ("kalshi:A", "polymarket_us:a")
        m = _match("A", "a")
        ref = _refresher(k_ids=[], p_ids=[], tracked=[m],
                         last_append={key: (1.0, 0.02, True, "polymarket_us")},
                         pair_id={key: 42}, writer=w)
        asyncio.run(ref.refresh_once())
        assert len(w.rows) == 1
        row = w.rows[0]
        assert row[0] == 42                 # match_pair_id
        assert row[1] == NOW                # observed_at
        assert row[2] == UNSUBSCRIBED       # and NOT WINDOW_CLOSE
        assert row[2] != WINDOW_CLOSE
        assert row[3] == "polymarket_us"    # the direction of the closed window
        assert row[13] == 0                 # fillable_size: reads as not viable
        assert row[4:10] == (None,) * 6     # no prices or fees were observed
        assert row[10:13] == (0.0, 0.0, 0.0)  # spreads: no measurement
        assert row[14:20] == (None,) * 6    # no depth walk happened

    def test_a_pair_that_was_not_viable_gets_no_marker(self, stub_db, match_file):
        # A close marker on a pair with no open window would invent a window.
        match_file([])
        w = _Writer()
        key = ("kalshi:A", "polymarket_us:a")
        ref = _refresher(k_ids=[], p_ids=[],
                         tracked=[_match("A", "a")],
                         last_append={key: (1.0, -0.01, False, "kalshi")},
                         pair_id={key: 42}, writer=w)
        asyncio.run(ref.refresh_once())
        assert w.rows == []

    def test_a_pair_with_no_db_row_is_skipped_rather_than_guessed(
            self, stub_db, match_file):
        # No match_pair id means the writer would drop the row anyway; inventing
        # one would attach a window's end to somebody else's pair.
        match_file([])
        w = _Writer()
        key = ("kalshi:A", "polymarket_us:a")
        ref = _refresher(k_ids=[], p_ids=[],
                         tracked=[_match("A", "a")],
                         last_append={key: (1.0, 0.02, True, "kalshi")},
                         pair_id={}, writer=w)
        asyncio.run(ref.refresh_once())
        assert w.rows == []

    def test_the_marker_is_censoring_not_closure_for_the_survival_curve(self):
        # The reason exists so the two are separable downstream: a window our own
        # refresh ended is censored, never counted as the market closing it.
        from pmarb.backtest.survival import survival_curve
        from pmarb.models import Sample

        def sample(ts, size, reason):
            return Sample(
                observed_at=datetime(2026, 9, 21, 12, 0, ts, tzinfo=UTC),
                market_a_id="kalshi:A", market_b_id="polymarket_us:a",
                spread_top=0.02, spread_depth=0.01,
                spread_fee_adj=0.005 if size else -0.001,
                fillable_size=size, sample_reason=reason)

        opened = [sample(0, 50, 2), sample(1, 0, UNSUBSCRIBED)]
        closed = [sample(0, 50, 2), sample(1, 0, WINDOW_CLOSE)]
        [unsub] = survival_curve(opened, deltas_ms=(1_000,))
        [obs] = survival_curve(closed, deltas_ms=(1_000,))
        assert (unsub.censored, unsub.opened) == (1, 0)   # fate unknown
        assert (obs.censored, obs.opened) == (0, 1)       # fate observed


class TestPruning:
    def test_dropped_pairs_leave_no_residue(self, stub_db, match_file):
        kept, gone = _match("A", "a"), _match("B", "b")
        match_file([kept, gone])
        key_gone = ("kalshi:B", "polymarket_us:b")
        key_kept = ("kalshi:A", "polymarket_us:a")
        last_append = {key_kept: (1.0, 0.01, False, "kalshi"),
                       key_gone: (1.0, 0.01, False, "kalshi")}
        cache = {"kalshi:A": 1, "polymarket_us:a": 1,
                 "kalshi:B": 1, "polymarket_us:b": 1}
        # B's Poly leg has settled, so the pair goes.
        ref = _refresher(
                         k_ids=["kalshi:A", "kalshi:B"],
                         p_ids=["polymarket_us:a"],
                         tracked=[kept, gone],
                         last_append=last_append, cache=cache)
        out = asyncio.run(ref.refresh_once())
        assert out["dropped"] == 1 and out["tracked"] == 1
        assert set(last_append) == {key_kept}
        assert set(cache) == {"kalshi:A", "polymarket_us:a"}
        assert set(ref._index) == {"kalshi:A", "polymarket_us:a"}
        # And the market lists the consumers restart from were corrected too, so
        # a consumer crash tomorrow does not resubscribe yesterday's slate.
        assert [m.id for m in ref._k_markets] == ["kalshi:A"]
        assert ref._kfeed.resynced == ["kalshi:A"]

    def test_added_pairs_are_indexed_under_both_legs(self, stub_db, match_file):
        old, new = _match("A", "a"), _match("B", "b")
        match_file([old, new])
        ref = _refresher(
                         k_ids=["kalshi:A", "kalshi:B"],
                         p_ids=["polymarket_us:a", "polymarket_us:b"],
                         tracked=[old])
        out = asyncio.run(ref.refresh_once())
        assert out["added"] == 1
        assert ref._index["kalshi:B"] == [new]
        assert ref._index["polymarket_us:b"] == [new]
        # One entry per leg and no duplicates: the hot loop iterates this list
        # per book update, so a double entry doubles every sample.
        assert all(len(v) == 1 for v in ref._index.values())

    def test_the_latest_snapshot_forgets_dropped_pairs(self, stub_db, match_file,
                                                       tmp_path):
        from pmarb.oplog import LatestOpportunityLog
        match_file([])
        log = LatestOpportunityLog(str(tmp_path / "latest.jsonl"),
                                   flush_interval=0.0)
        log._latest[("kalshi:A", "polymarket_us:a")] = {"x": 1}
        ref = _refresher(k_ids=[], p_ids=[],
                         tracked=[_match("A", "a")], latest_log=log)
        asyncio.run(ref.refresh_once())
        assert log.count == 0        # a settled game is not "open right now"
        log.close()

    def test_pair_id_is_updated_in_place(self, match_file, monkeypatch):
        # The hot loop closed over this dict. Rebinding it here would leave the
        # detector reading the old mapping — a drift, not a failure.
        match_file([])
        monkeypatch.setattr(MatchSetRefresher, "_sync_db",
                            lambda self, m, a: {("kalshi:Z", "polymarket_us:z"): 9})
        pair_id = {("kalshi:A", "polymarket_us:a"): 1}
        ref = _refresher(k_ids=[], p_ids=[], tracked=[],
                         pair_id=pair_id)
        asyncio.run(ref.refresh_once())
        assert pair_id == {("kalshi:Z", "polymarket_us:z"): 9}

    def test_a_database_failure_does_not_abandon_the_refresh(self, match_file):
        # Database problems must never stop collection: the drops still apply,
        # and `pair_id` is left alone rather than half-rewritten.
        match_file([])

        def boom(self, markets, all_matches):
            raise RuntimeError("no database")

        pair_id = {("kalshi:A", "polymarket_us:a"): 1}
        ref = _refresher(k_ids=[], p_ids=[],
                         tracked=[_match("A", "a")], pair_id=pair_id)
        ref._sync_db = boom.__get__(ref, MatchSetRefresher)
        out = asyncio.run(ref.refresh_once())
        assert out["dropped"] == 1
        assert pair_id == {("kalshi:A", "polymarket_us:a"): 1}

    def test_a_failed_fetch_changes_nothing(self, stub_db, match_file):
        # Step 1 is deliberately before every mutation, so a network blip at
        # 11:00 costs a day of freshness and nothing else.
        match_file([])

        async def boom(feed):
            raise OSError("dns")

        m = _match("A", "a")
        ref = _refresher(k_ids=[], p_ids=[], tracked=[m],
                         fetch=boom)
        with pytest.raises(OSError):
            asyncio.run(ref.refresh_once())
        assert set(ref._index) == {"kalshi:A", "polymarket_us:a"}


class TestSchedule:
    """The clock is injectable because a schedule that can only be exercised by
    waiting until 11:00 UTC is a schedule nobody exercises."""

    def test_seconds_until_the_next_hour(self):
        assert seconds_until_hour(datetime(2026, 9, 21, 10, 0, tzinfo=UTC), 11) \
            == 3600.0
        # Already past it today -> tomorrow, not a negative sleep.
        assert seconds_until_hour(datetime(2026, 9, 21, 12, 0, tzinfo=UTC), 11) \
            == 23 * 3600.0
        # Exactly on the hour waits a full day rather than re-firing at once.
        assert seconds_until_hour(datetime(2026, 9, 21, 11, 0, tzinfo=UTC), 11) \
            == 86_400.0

    def test_the_loop_sleeps_until_the_hour_then_refreshes(self, stub_db,
                                                           match_file):
        match_file([])
        slept = []
        ref = _refresher(k_ids=[], p_ids=[], tracked=[])
        cycles = []
        ref.refresh_once = lambda: cycles.append(1) or _summary()
        # The fake clock advances with the fake sleeps, so the schedule is
        # exercised over a day boundary without one existing.
        clock = [NOW]
        ref._clock = lambda: clock[0]

        async def sleep(d):
            slept.append(d)
            clock[0] += timedelta(seconds=d)

        asyncio.run(ref.run(hour=11, sleep=sleep, cycles=2))
        # NOW is 09:30 UTC -> 1.5h to the first cycle, a day to the next.
        assert slept == [5400.0, 86_400.0]
        assert len(cycles) == 2

    def test_an_interval_override_drives_it_in_tests(self, stub_db, match_file):
        match_file([])
        ref = _refresher(k_ids=[], p_ids=[], tracked=[])
        slept = []

        async def sleep(d):
            slept.append(d)

        asyncio.run(ref.run(interval=0.0, sleep=sleep, cycles=3))
        assert slept == [0.0, 0.0, 0.0]
        assert ref.cycles == 3

    def test_a_failing_cycle_does_not_kill_the_loop(self, stub_db, match_file):
        # A refresh failing costs a day of stale books. The loop dying costs the
        # run — and would look fine from the outside.
        match_file([])
        ref = _refresher(k_ids=[], p_ids=[], tracked=[])
        calls = []

        async def boom():
            calls.append(1)
            raise RuntimeError("catalog fetch exploded")

        ref.refresh_once = boom

        async def sleep(d):
            return None

        asyncio.run(ref.run(interval=0.0, sleep=sleep, cycles=3))
        assert len(calls) == 3


async def _summary():
    return {"added": 0, "dropped": 0, "tracked": 0, "closed": 0,
            "kalshi_books": 0, "poly_books": 0}
