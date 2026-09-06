"""Player-prop matching.

The identity is (game, player, stat, threshold) and every part of it is load
bearing: the same player has a market at 2+, 3+ and 4+ of the same stat in the
same game, and adjacent rungs are different contracts whose prices differ
precisely because the events do.

Both venues write the "at least N" side as YES, so unlike spreads there is no
orientation to resolve — but each feed must REFUSE anything that isn't that
side rather than assume it.
"""

from datetime import UTC, datetime, timedelta
from typing import ClassVar

from pmarb.feeds.kalshi import _prop_event as kalshi_prop
from pmarb.feeds.polymarket import _prop_event as poly_prop
from pmarb.matching.props import PropMatcher
from pmarb.models import Market, PropEvent

START = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)


def _mkt(mid, platform, prop) -> Market:
    return Market(
        id=mid, platform=platform, question="q",
        resolution_date=START + timedelta(days=2), category="sports",
        yes_depth=(), no_depth=(), updated_at=START, prop=prop,
    )


def _prop(player="Kenneth Walker III", stat="receiving_yards", threshold=15.0,
          comps=("Denver", "Kansas City"), start=START, league="nfl"):
    return PropEvent(league=league, start_time=start, competitors=comps,
                     player=player, stat=stat, threshold=threshold)


def _k(mid="kalshi:K1", **over):
    return _mkt(mid, "kalshi", _prop(**over))


def _p(mid="polymarket_us:P1", **over):
    over.setdefault("comps", ("Denver Broncos", "Kansas City Chiefs"))
    return _mkt(mid, "polymarket_us", _prop(**over))


class TestKalshiExtraction:
    BASE: ClassVar[dict] = {"ticker": "KXNFLRECYDS-26SEP14DENKC-KCKWALKER9-15",
            "yes_sub_title": "Kenneth Walker III: 15+",
            "floor_strike": 14.5, "strike_type": "greater"}
    EVENT: ClassVar[dict] = {"title": "Denver vs Kansas City: Receiving Yards"}

    def test_reads_player_stat_threshold_and_game(self):
        pe = kalshi_prop(self.BASE, self.EVENT)
        assert pe.player == "Kenneth Walker III"
        assert pe.stat == "receiving_yards"
        assert pe.threshold == 15.0          # the rung, not floor_strike
        assert pe.competitors == ("Denver", "Kansas City")

    def test_threshold_disagreeing_with_floor_strike_is_refused(self):
        # "15+" and floor_strike 14.5 are one number written twice. If they
        # disagree the wire format moved and the market is not understood.
        assert kalshi_prop({**self.BASE, "floor_strike": 20.5},
                           self.EVENT) is None

    def test_a_prop_is_not_an_outright(self):
        # The bug that hid this whole block: "Cal Raleigh: 2+" reads as an
        # entity, so every prop was classified as a futures market.
        from pmarb.feeds.kalshi import _market_metadata
        m = _market_metadata({**self.BASE, "title": "x",
                              "close_time": "2026-09-17T00:15:00Z"},
                             START, self.EVENT)
        assert m.prop is not None
        assert m.futures is None and m.line is None


class TestPolyExtraction:
    BASE: ClassVar[dict] = {
        "sportsMarketType": "baseball_player_total_bases", "line": 2,
        "title": "Jackson Chourio", "gameStartTime": "2026-09-05T22:40:00Z",
        "marketSides": [{"description": "Yes", "long": True},
                        {"description": "No", "long": False}],
        "description": ("This market will settle to Yes if Jackson Chourio "
                        "records 2 or more total bases in the Milwaukee Brewers "
                        "vs Cincinnati Reds MLB game scheduled for 2026-09-05."),
    }

    def test_reads_player_stat_threshold_and_game(self):
        pe = poly_prop(self.BASE)
        assert pe.player == "Jackson Chourio" and pe.stat == "total_bases"
        assert pe.threshold == 2.0
        assert pe.competitors == ("Milwaukee Brewers", "Cincinnati Reds")

    def test_a_non_yes_long_side_is_refused(self):
        # YES must be the "at least N" side, which is the side Kalshi writes.
        assert poly_prop({**self.BASE, "marketSides": [
            {"description": "Under", "long": True},
            {"description": "Over", "long": False}]}) is None

    def test_an_unparseable_game_is_refused(self):
        assert poly_prop({**self.BASE,
                          "description": "Settles per official scoring."}) is None


class TestThresholdIsPartOfTheContract:
    def test_adjacent_rungs_do_not_match(self):
        assert PropMatcher().match([_k(threshold=2.0)],
                                   [_p(threshold=3.0)]) == []

    def test_the_same_rung_matches(self):
        out = PropMatcher().match([_k(threshold=2.0)], [_p(threshold=2.0)])
        assert len(out) == 1 and out[0].match_method == "prop"

    def test_stats_do_not_cross(self):
        # Receiving yards and receptions are different bets on one player.
        assert PropMatcher().match([_k(stat="receiving_yards")],
                                   [_p(stat="receptions")]) == []


class TestGameVerification:
    def test_a_different_game_does_not_match(self):
        assert PropMatcher().match(
            [_k()], [_p(comps=("Chicago Bears", "Carolina Panthers"))]) == []

    def test_a_different_player_does_not_match(self):
        assert PropMatcher().match([_k()], [_p(player="Mike Evans")]) == []

    def test_the_same_prop_in_a_later_game_is_a_different_contract(self):
        # A player's 2+ hits prop exists on both days of a series; each Kalshi
        # market must take its own listing.
        comps_k, comps_p = ("Washington", "Los Angeles D"), (
            "Washington Nationals", "Los Angeles Dodgers")
        day2 = START + timedelta(hours=24)
        k = [_k("kalshi:G1", stat="hits", threshold=2.0, comps=comps_k,
                start=START, league="mlb"),
             _k("kalshi:G2", stat="hits", threshold=2.0, comps=comps_k,
                start=day2, league="mlb")]
        p = [_p("polymarket_us:G1", stat="hits", threshold=2.0, comps=comps_p,
                start=START, league="mlb"),
             _p("polymarket_us:G2", stat="hits", threshold=2.0, comps=comps_p,
                start=day2, league="mlb")]
        out = PropMatcher().match(k, p)
        assert {c.kalshi_id: c.polymarket_id for c in out} == {
            "kalshi:G1": "polymarket_us:G1", "kalshi:G2": "polymarket_us:G2"}

    def test_a_surname_only_listing_still_aligns(self):
        # Kalshi abbreviates city names ("Los Angeles D"); the subset rule in
        # competitor_score is what carries that across.
        out = PropMatcher().match(
            [_k(stat="hits", comps=("Washington", "Los Angeles D"), league="mlb")],
            [_p(stat="hits", comps=("Washington Nationals", "Los Angeles Dodgers"),
                league="mlb")])
        assert len(out) == 1
