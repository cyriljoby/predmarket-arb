"""Tests for evaluate_pair (funnel record) and the opportunity logger."""

import json
from datetime import datetime, timezone

import pytest

from pmarb.detection.spread import PairEvaluation, evaluate_pair
from pmarb.oplog.logger import (
    LatestOpportunityLog,
    OpportunityLogger,
    opportunity_record,
)
from pmarb.models import Market, PriceLevel

NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)


def mkt(platform, mid, yes_depth, no_depth):
    return Market(
        id=f"{platform}:{mid}", platform=platform, question="Q",
        resolution_date=NOW, category="", updated_at=NOW,
        yes_depth=tuple(PriceLevel(*l) for l in yes_depth),
        no_depth=tuple(PriceLevel(*l) for l in no_depth),
    )


class TestEvaluatePair:
    def test_none_when_no_top_of_book_edge(self):
        # both asks sum > 1 in both directions -> no apparent edge
        k = mkt("kalshi", "K", [(0.60, 100)], [(0.50, 100)])
        p = mkt("polymarket_us", "P", [(0.60, 100)], [(0.50, 100)])
        assert evaluate_pair(k, p, NOW) is None

    def test_none_when_stale(self):
        k = mkt("kalshi", "K", [(0.40, 100)], [(0.55, 100)])
        p = mkt("polymarket_us", "P", [(0.40, 100)], [(0.55, 100)])
        stale = datetime(2026, 7, 6, 0, 0, 30, tzinfo=timezone.utc)  # 30s later
        assert evaluate_pair(k, p, stale, max_staleness=2.0) is None

    def test_records_apparent_edge_not_fee_viable(self):
        # YES Kalshi 0.49 + NO Poly 0.50 = 0.99 -> 1c top edge, but ~3c fees at
        # mid-price eat it -> size 0, fee_adjusted from a single contract.
        k = mkt("kalshi", "K", [(0.49, 1)], [(0.51, 1)])
        p = mkt("polymarket_us", "P", [(0.51, 1)], [(0.50, 1)])
        ev = evaluate_pair(k, p, NOW)
        assert ev is not None
        assert ev.raw_spread_top_of_book == pytest.approx(0.01)
        # not fee-viable at any size -> depth-adjusted == top-of-book (no slippage)
        assert ev.estimated_fillable_size == 0
        assert ev.raw_spread_depth_adjusted == ev.raw_spread_top_of_book
        assert ev.fee_adjusted_spread < 0  # 1c edge minus ~3c fees

    def test_viable_uses_depth_walked_economics(self):
        # deep, wide edge: YES Kalshi 0.40 + NO Poly 0.40 = 0.80 -> 20c, fillable
        k = mkt("kalshi", "K", [(0.40, 500)], [(0.60, 500)])
        p = mkt("polymarket_us", "P", [(0.60, 500)], [(0.40, 500)])
        ev = evaluate_pair(k, p, NOW)
        assert ev is not None and ev.estimated_fillable_size > 0
        assert ev.fee_adjusted_spread > 0
        # depth-adjusted is between top-of-book and fee-adjusted
        assert ev.fee_adjusted_spread < ev.raw_spread_depth_adjusted


class TestLogger:
    def _ev(self):
        return PairEvaluation(
            yes_platform="kalshi", no_platform="polymarket_us",
            yes_ask_top=0.42, no_ask_top=0.55, estimated_fillable_size=47,
            yes_fill_price=0.431, no_fill_price=0.558,
            raw_spread_top_of_book=0.03, raw_spread_depth_adjusted=0.011,
            yes_fee_per_share=0.0172, no_fee_per_share=0.01,
            fee_adjusted_spread=0.0028,
        )

    def test_record_has_all_three_spread_fields(self):
        match = {"kalshi_id": "kalshi:K1", "polymarket_id": "polymarket_us:P1",
                 "kalshi_question": "Q?", "match_method": "structured",
                 "resolution_match": True}
        rec = opportunity_record(self._ev(), match, now=NOW)
        for f in ("raw_spread_top_of_book", "raw_spread_depth_adjusted",
                  "fee_adjusted_spread"):
            assert f in rec
        assert "slippage_adjusted_profit" not in rec  # forbidden field
        assert rec["strategy"] == "YES_KALSHI_NO_POLYMARKET_US"
        assert rec["estimated_fillable_size"] == 47
        assert rec["resolution_match"] is True

    def test_append_writes_every_event(self, tmp_path):
        match = {"kalshi_id": "k", "polymarket_id": "p", "kalshi_question": "Q",
                 "match_method": "futures", "resolution_match": None}
        path = tmp_path / "opps.jsonl"
        with OpportunityLogger(str(path)) as log:
            log.log(self._ev(), match, now=NOW)
            log.log(self._ev(), match, now=NOW)
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2  # append: every event is a line
        assert json.loads(lines[0])["fee_adjusted_spread"] == 0.0028


class TestLatestLog:
    def _ev(self, spread):
        return PairEvaluation(
            yes_platform="kalshi", no_platform="polymarket_us",
            yes_ask_top=0.4, no_ask_top=0.55, estimated_fillable_size=10,
            yes_fill_price=0.4, no_fill_price=0.55,
            raw_spread_top_of_book=0.05, raw_spread_depth_adjusted=0.05,
            yes_fee_per_share=0.01, no_fee_per_share=0.01,
            fee_adjusted_spread=spread,
        )

    def test_same_pair_is_overwritten_not_duplicated(self, tmp_path):
        m = {"kalshi_id": "k1", "polymarket_id": "p1", "kalshi_question": "Q",
             "match_method": "futures", "resolution_match": None}
        path = tmp_path / "latest.jsonl"
        with LatestOpportunityLog(str(path), flush_interval=0.0) as log:
            log.log(self._ev(0.01), m, now=NOW)
            log.log(self._ev(0.02), m, now=NOW)  # same pair -> overwrite
            log.log(self._ev(0.03), m, now=NOW)  # keep most recent
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1  # one line per pair
        assert json.loads(lines[0])["fee_adjusted_spread"] == 0.03  # latest wins

    def test_distinct_pairs_each_get_a_line(self, tmp_path):
        m1 = {"kalshi_id": "k1", "polymarket_id": "p1", "kalshi_question": "Q1",
              "match_method": "futures", "resolution_match": None}
        m2 = {"kalshi_id": "k2", "polymarket_id": "p2", "kalshi_question": "Q2",
              "match_method": "structured", "resolution_match": None}
        path = tmp_path / "latest.jsonl"
        with LatestOpportunityLog(str(path), flush_interval=0.0) as log:
            log.log(self._ev(0.01), m1, now=NOW)
            log.log(self._ev(0.02), m2, now=NOW)
        assert len(path.read_text().strip().splitlines()) == 2
        assert log.count == 2
