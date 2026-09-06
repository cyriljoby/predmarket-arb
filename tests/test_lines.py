"""Spread/total matching, where the dangerous failure is ORIENTATION.

A wrong moneyline pair is two unrelated bets. A wrong-orientation spread pair is
worse: it looks like a textbook hedge while being the SAME bet twice, so the
detector prices a guaranteed profit that does not exist. Most of these tests
exist to pin that down.

The truth table, with Kalshi always writing the laying side ("T wins by over L"):

    Poly YES        Kalshi market      valid?   orientation
    T at -L         T wins by over L   yes      direct
    T at +L         opponent by over L yes      inverted
    T at +L         T wins by over L   NO       refused
    T at -L         opponent by over L NO       refused
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from pmarb.main import oriented
from pmarb.matching.lines import LineMatcher, _align, _orientation
from pmarb.models import LineEvent, Market, PriceLevel

START = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)


def _mkt(mid, platform, line_event, question="q") -> Market:
    return Market(
        id=mid, platform=platform, question=question,
        resolution_date=START + timedelta(days=1), category="sports",
        yes_depth=(), no_depth=(), updated_at=START, line=line_event,
    )


def _kalshi(team, line=5.5, kind="spread", comps=("Denver", "Kansas City"),
            start=START, league="nfl", mid="kalshi:K1"):
    return _mkt(mid, "kalshi", LineEvent(
        league=league, start_time=start, competitors=comps, kind=kind,
        line=line, yes_team=team, yes_favored=True))


def _poly(team, favored, line=5.5, kind="spread",
          comps=("Denver Broncos", "Kansas City Chiefs"), start=START,
          league="nfl", mid="polymarket_us:P1"):
    return _mkt(mid, "polymarket_us", LineEvent(
        league=league, start_time=start, competitors=comps, kind=kind,
        line=line, yes_team=team, yes_favored=favored))


class TestOrientation:
    """Which Kalshi market a given Poly side may legally pair with."""

    def test_favored_side_pairs_directly_with_its_own_team(self):
        km, pm = _kalshi("Denver"), _poly("Denver Broncos", favored=True)
        mapping = _align(km.line.competitors, pm.line.competitors)
        assert _orientation(km, pm, mapping) is False

    def test_underdog_side_pairs_inverted_with_the_opponent(self):
        # Poly "Denver +5.5" is the exact complement of Kalshi "Kansas City
        # wins by over 5.5" — buy both YESes and exactly one pays.
        km, pm = _kalshi("Kansas City"), _poly("Denver Broncos", favored=False)
        mapping = _align(km.line.competitors, pm.line.competitors)
        assert _orientation(km, pm, mapping) is True

    def test_underdog_against_its_own_team_is_refused(self):
        # THE bug this table exists for. "Denver +5.5" and "Denver wins by over
        # 5.5" share a team and a number and are different bets: Denver losing
        # by 3 settles the first YES and the second NO, and Denver winning by 40
        # settles both YES. Neither equal nor complementary — no hedge.
        km, pm = _kalshi("Denver"), _poly("Denver Broncos", favored=False)
        mapping = _align(km.line.competitors, pm.line.competitors)
        assert _orientation(km, pm, mapping) is None

    def test_favored_against_the_opponent_is_refused(self):
        km, pm = _kalshi("Kansas City"), _poly("Denver Broncos", favored=True)
        mapping = _align(km.line.competitors, pm.line.competitors)
        assert _orientation(km, pm, mapping) is None

    def test_totals_are_always_direct(self):
        km = _kalshi("", kind="total", line=45.5)
        pm = _poly("", favored=True, kind="total", line=45.5)
        mapping = _align(km.line.competitors, pm.line.competitors)
        assert _orientation(km, pm, mapping) is False


class TestTheLineIsPartOfTheIdentity:
    def test_different_lines_never_match(self):
        # 45.5 and 46.5 on the same game are different contracts; pairing them
        # invents an edge out of two unrelated bets.
        k = [_kalshi("Denver", line=5.5)]
        p = [_poly("Denver Broncos", favored=True, line=6.5)]
        assert LineMatcher().match(k, p) == []

    def test_same_line_matches(self):
        k = [_kalshi("Denver", line=5.5)]
        p = [_poly("Denver Broncos", favored=True, line=5.5)]
        out = LineMatcher().match(k, p)
        assert len(out) == 1 and out[0].poly_inverted is False
        assert out[0].match_method == "line"

    def test_spreads_and_totals_do_not_cross(self):
        k = [_kalshi("Denver", kind="spread", line=5.5)]
        p = [_poly("", favored=True, kind="total", line=5.5)]
        assert LineMatcher().match(k, p) == []

    def test_leagues_do_not_cross(self):
        k = [_kalshi("Denver", league="nfl")]
        p = [_poly("Denver Broncos", favored=True, league="cfb")]
        assert LineMatcher().match(k, p) == []


class TestGameIdentity:
    def test_a_game_outside_the_start_window_is_not_the_same_game(self):
        k = [_kalshi("Denver", start=START)]
        p = [_poly("Denver Broncos", favored=True,
                   start=START + timedelta(hours=40))]
        assert LineMatcher().match(k, p) == []

    def test_unrelated_teams_do_not_align(self):
        k = [_kalshi("Denver", comps=("Denver", "Kansas City"))]
        p = [_poly("Chicago Bears", favored=True,
                   comps=("Chicago Bears", "Carolina Panthers"))]
        assert LineMatcher().match(k, p) == []

    def test_consecutive_games_resolve_one_to_one_by_closest_start(self):
        # Arizona at Houston on back-to-back days: same teams, same line, both
        # inside the 30h window. Each Kalshi game must take its OWN listing,
        # not both take the nearest one.
        comps_k, comps_p = ("Arizona", "Houston"), ("Arizona Diamondbacks",
                                                    "Houston Astros")
        day1, day2 = START, START + timedelta(hours=24)
        k = [_kalshi("", kind="total", line=6.5, comps=comps_k, start=day1,
                     league="mlb", mid="kalshi:D1"),
             _kalshi("", kind="total", line=6.5, comps=comps_k, start=day2,
                     league="mlb", mid="kalshi:D2")]
        p = [_poly("", favored=True, kind="total", line=6.5, comps=comps_p,
                   start=day1, league="mlb", mid="polymarket_us:D1"),
             _poly("", favored=True, kind="total", line=6.5, comps=comps_p,
                   start=day2, league="mlb", mid="polymarket_us:D2")]
        out = LineMatcher().match(k, p)
        assert len({c.kalshi_id for c in out}) == len(out)
        assert len({c.polymarket_id for c in out}) == len(out)
        paired = {c.kalshi_id: c.polymarket_id for c in out}
        assert paired == {"kalshi:D1": "polymarket_us:D1",
                          "kalshi:D2": "polymarket_us:D2"}


class TestTheCollectorAppliesOrientation:
    """The matcher deciding `poly_inverted` is only half the job — the detector
    has to be handed a leg whose YES matches Kalshi's, or the flag is decoration
    on a pair that still prices a phantom hedge."""

    @staticmethod
    def _with_book(favored: bool) -> Market:
        return replace(_poly("Denver Broncos", favored=favored),
                       yes_depth=(PriceLevel(0.40, 10),),
                       no_depth=(PriceLevel(0.62, 20),),
                       yes_bid=0.38, no_bid=0.58)

    def test_a_direct_pair_is_passed_through_untouched(self):
        p = self._with_book(favored=True)
        assert oriented(p, {"poly_inverted": False}) is p
        assert oriented(p, {}) is p          # absent flag means direct

    def test_an_inverted_pair_has_its_ladders_swapped(self):
        p = self._with_book(favored=False)
        out = oriented(p, {"poly_inverted": True})
        assert out.yes_depth == p.no_depth
        assert out.no_depth == p.yes_depth
        assert (out.yes_bid, out.no_bid) == (0.58, 0.38)

    def test_the_original_market_is_not_mutated(self):
        # One Market object is shared by every pair that references it; an
        # in-place swap would corrupt the other pairs' view of the same book.
        p = self._with_book(favored=False)
        oriented(p, {"poly_inverted": True})
        assert p.yes_depth == (PriceLevel(0.40, 10),)
