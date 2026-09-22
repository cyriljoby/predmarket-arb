"""The file reader and the database must be interchangeable inputs.

`run_backtest(rows, matches, tracked)` computes on `Sample`, so there is exactly
one implementation of the funnel and one vocabulary. The database is canonical:
its column names ARE the field names. The JSONL reader is the adapter, because
the Phase 1 log is venue-specific (kalshi_/polymarket_) and translating the
generic schema into that shape would re-couple the analysis to two venues.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pmarb.backtest.backtest import _sample_from_log, run_backtest
from pmarb.db.sources import _to_sample
from pmarb.models import Sample

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
PAIR = ("kalshi:A", "polymarket_us:B")


def _sample(**over) -> Sample:
    base = dict(observed_at=NOW, market_a_id=PAIR[0], market_b_id=PAIR[1],
                spread_top=0.03, spread_depth=0.011, spread_fee_adj=0.0028,
                fillable_size=100, question="Will it?", match_method="futures",
                fee_yes=0.0172, fee_no=0.01)
    return Sample(**{**base, **over})


def _match(**over):
    m = {"kalshi_id": PAIR[0], "polymarket_id": PAIR[1], "match_method": "futures",
         "similarity_score": 1.0, "resolution_match": True,
         "review_sample": "unbiased-tracked-x"}
    m.update(over)
    return m


class TestSampleContract:
    def test_viable_requires_both_positive_edge_and_size(self):
        assert _sample(spread_fee_adj=0.01, fillable_size=5).viable
        assert not _sample(spread_fee_adj=0.01, fillable_size=0).viable
        assert not _sample(spread_fee_adj=-0.01, fillable_size=5).viable

    def test_settles_at_takes_the_later_leg(self):
        # Capital is locked until BOTH legs settle, so the holding period is
        # bounded by the later date, not the nearer one.
        a, b = NOW + timedelta(days=30), NOW + timedelta(days=400)
        assert _sample(resolution_date_a=a, resolution_date_b=b).settles_at == b
        assert _sample(resolution_date_a=b, resolution_date_b=a).settles_at == b

    def test_settles_at_is_none_when_neither_leg_has_a_date(self):
        assert _sample().settles_at is None

    def test_samples_are_immutable(self):
        # Labels are looked up from the current match set rather than written
        # onto rows: a verdict can be revised after a sample is recorded.
        with pytest.raises(Exception):
            _sample().spread_fee_adj = 0.5


class TestBothSourcesAgree:
    """A database row and the equivalent log line must produce the same Sample."""

    DB_ROW = {
        "observed_at": NOW, "market_a_id": PAIR[0], "market_b_id": PAIR[1],
        "question": "Will it?", "match_method": "futures", "yes_venue": "kalshi",
        "fillable_size": 100, "sample_reason": 2,
        "yes_ask_top": Decimal("0.4200"), "no_ask_top": Decimal("0.5500"),
        "yes_fill_price": Decimal("0.4310"), "no_fill_price": Decimal("0.5580"),
        "yes_fee": Decimal("0.01720"), "no_fee": Decimal("0.01000"),
        "spread_top": Decimal("0.03000"), "spread_depth": Decimal("0.01100"),
        "spread_fee_adj": Decimal("0.00280"),
        "resolution_date_a": None, "resolution_date_b": None,
        "detect_latency_ms": Decimal("0.412"), "partner_age_ms": 180,
    }
    LOG_LINE = {
        "timestamp": NOW.isoformat(), "kalshi_market_id": PAIR[0],
        "polymarket_market_id": PAIR[1], "question": "Will it?",
        "match_method": "futures", "yes_platform": "kalshi",
        "estimated_fillable_size": 100,
        "yes_ask_top": 0.42, "no_ask_top": 0.55,
        "yes_fill_price": 0.431, "no_fill_price": 0.558,
        "yes_fee_per_share": 0.0172, "no_fee_per_share": 0.01,
        "raw_spread_top_of_book": 0.03, "raw_spread_depth_adjusted": 0.011,
        "fee_adjusted_spread": 0.0028,
    }

    def test_equivalent_inputs_produce_equal_samples(self):
        db, log = _to_sample(self.DB_ROW), _sample_from_log(self.LOG_LINE)
        # sample_reason is the one honest difference: the legacy log predates it.
        assert db.sample_reason == 2
        assert log.sample_reason is None
        for field in ("observed_at", "market_a_id", "market_b_id", "spread_top",
                      "spread_depth", "spread_fee_adj", "fillable_size",
                      "question", "match_method", "yes_venue", "fee_yes"):
            assert getattr(db, field) == getattr(log, field), field

    def test_decimals_become_floats_at_the_boundary(self):
        s = _to_sample(self.DB_ROW)
        assert isinstance(s.spread_fee_adj, float)
        assert s.spread_fee_adj == 0.0028

    def test_legacy_log_accepts_the_older_pair_key_spelling(self):
        line = dict(self.LOG_LINE)
        line["polymarket_id"] = line.pop("polymarket_market_id")
        assert _sample_from_log(line).pair == PAIR

    def test_both_run_through_the_funnel_identically(self):
        db = run_backtest([_to_sample(self.DB_ROW)], [_match()], {PAIR}, source="db")
        log = run_backtest([_sample_from_log(self.LOG_LINE)], [_match()],
                           {PAIR}, source="file")
        db.pop("source"), log.pop("source")
        # Latency is the second honest difference between the sources (after
        # sample_reason): the JSONL sinks never carried detect_latency_ms, so
        # the file path cannot report it. Everything the funnel computes must
        # still agree exactly.
        db_lat, log_lat = db.pop("latency"), log.pop("latency")
        assert db == log
        assert db_lat["rows_with_latency"] == 1
        assert log_lat["rows_with_latency"] == 0
        assert log_lat["detect_ms"]["p50"] is None


class TestRunBacktestIsPure:
    def test_same_inputs_give_the_same_report(self):
        args = ([_sample()], [_match()], {PAIR})
        a, b = run_backtest(*args, source="one"), run_backtest(*args, source="two")
        a.pop("source"), b.pop("source")
        assert a == b

    def test_retracted_pairs_leave_every_numerator_and_denominator(self):
        # A pair absent from `matches` was never a hedge; its samples must not
        # count anywhere.
        rows = [_sample(), _sample(market_a_id="kalshi:GONE")]
        rep = run_backtest(rows, [_match()], source="t")
        assert rep["samples"] == 1
        assert rep["retracted_by_match_set"]["pairs"] == 1


class TestAnnualisedReturn:
    """The edge is time-blind; this is what makes windows comparable."""

    def _s(self, days, **over):
        base = dict(fill_yes=0.42, fill_no=0.57, spread_fee_adj=0.01,
                    resolution_date_a=NOW + timedelta(days=days))
        return _sample(**{**base, **over})

    def test_short_dated_edge_beats_an_identical_long_dated_one(self):
        # 1c settling in a week is worth vastly more than 1c settling in 3 years,
        # and spread_fee_adj alone ranks them equal.
        assert self._s(7).annualised_return > 40 * self._s(1095).annualised_return

    def test_uses_the_later_leg(self):
        # Capital is locked until both settle, so the nearer date must not win.
        s = self._s(30, resolution_date_b=NOW + timedelta(days=400))
        assert s.days_to_settlement == 400

    def test_is_a_rate_on_capital_not_on_notional(self):
        # 1c edge on 99c of cost over a year is ~1%/yr, not 1c/yr.
        s = self._s(365, fill_yes=0.42, fill_no=0.57, spread_fee_adj=0.01)
        assert s.annualised_return == pytest.approx(0.01 / 0.99, rel=1e-6)

    def test_sub_day_horizons_floor_at_one_day(self):
        # Otherwise a window on a market settling in hours divides by zero and
        # reports an infinite rate.
        assert self._s(0).annualised_return == pytest.approx(0.01 / 0.99 * 365)

    def test_none_without_a_settlement_date(self):
        # Legacy JSONL rows predate the captured dates — unknowable, not zero.
        assert _sample(fill_yes=0.42, fill_no=0.57).annualised_return is None

    def test_none_without_fill_prices(self):
        assert self._s(30, fill_yes=None).annualised_return is None
