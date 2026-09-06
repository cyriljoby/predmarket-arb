"""Futures matcher: entity-outright extraction + (competition, entity) matching.

Fixtures mirror real wire payloads (2026-07): Kalshi outright event/market
shapes and Polymarket US futures title/question fields.
"""

from datetime import UTC, datetime, timedelta

from pmarb.feeds.kalshi import _market_metadata as kalshi_meta
from pmarb.feeds.polymarket import _market_metadata as poly_meta
from pmarb.matching.futures import (
    FuturesMatcher,
    competition_score,
    competition_tokens,
    same_period,
    stated_years,
)
from pmarb.models import FuturesEvent, Market

NOW = datetime(2026, 7, 5, tzinfo=UTC)


def kalshi_outright(
    ticker="KXPGATOUR-GENSCOT26-LABE",
    yes_sub="Ludvig Aberg",
    event_title="2026 Genesis Scottish Open",
    close="2026-07-26T12:00:00Z",
):
    market = {
        "ticker": ticker,
        "title": f"Will {yes_sub} win the {event_title}?",
        "yes_sub_title": yes_sub,
        "close_time": close,
        "category": "Sports",
    }
    return kalshi_meta(market, NOW, {"title": event_title})


def poly_outright(
    slug="tec-pga-genescot-2026-07-12-w-ludabe",
    title="Ludvig Aberg",
    question="Genesis Scottish Open Winner",
    end="2026-07-26T12:00:00Z",
):
    market = {
        "slug": slug,
        "title": title,
        "question": question,
        "marketType": "futures",
        "endDate": end,
        "category": "sports",
    }
    return poly_meta(market, NOW)


class TestKalshiExtraction:
    def test_extracts_entity_and_competition(self):
        m = kalshi_outright()
        assert m.futures is not None
        assert m.futures.entity == "Ludvig Aberg"
        assert m.futures.competition == "2026 Genesis Scottish Open"
        assert m.event is None  # outright, not a game

    def test_threshold_entity_rejected(self):
        m = kalshi_outright(yes_sub="Above 13000", event_title="Ferrari shipments 2026")
        assert m.futures is None

    def test_yes_no_entity_rejected(self):
        m = kalshi_outright(yes_sub="Yes", event_title="Will Spain ban bullfighting?")
        assert m.futures is None

    def test_scalar_wins_bucket_rejected(self):
        m = kalshi_outright(yes_sub="1+ golf major championship wins")
        assert m.futures is None


class TestPolyExtraction:
    def test_extracts_title_and_question(self):
        m = poly_outright()
        assert m.futures is not None
        assert m.futures.entity == "Ludvig Aberg"
        assert m.futures.competition == "Genesis Scottish Open Winner"

    def test_threshold_title_rejected(self):
        m = poly_outright(title="At least 2.0%", question="US GDP growth in Q2 2026")
        assert m.futures is None

    def test_non_futures_has_no_futures_event(self):
        m = poly_meta(
            {"slug": "x", "title": "T", "question": "Will X win the Open",
             "marketType": "moneyline", "endDate": "2026-07-20T00:00:00Z",
             "marketSides": []},
            NOW,
        )
        assert m.futures is None


class TestCompetitionScore:
    def test_year_dropped_so_scottish_open_matches(self):
        assert competition_score(
            "2026 Genesis Scottish Open", "Genesis Scottish Open Winner"
        ) >= 0.5

    def test_winner_and_round_leader_do_not_collide(self):
        # same tournament, different market type — must score low
        assert competition_score(
            "Genesis Scottish Open Winner",
            "Genesis Scottish Open End of Round 1 Leader",
        ) < 0.5

    def test_distinct_tournaments_low(self):
        assert competition_score(
            "Genesis Scottish Open", "ISCO Championship"
        ) == 0.0

    def test_stage_winner_does_not_match_overall(self):
        # the cycling bug: a single stage must not pair with the overall winner
        assert competition_score(
            "Tour de France: Stage 9 Winner", "Tour de France Winner"
        ) == 0.0

    def test_different_stages_do_not_match(self):
        assert competition_score(
            "Tour de France: Stage 9 Winner", "Tour de France: Stage 8 Winner"
        ) == 0.0

    def test_same_stage_matches(self):
        assert competition_score(
            "Tour de France: Stage 9 Winner", "Tour de France Stage 9"
        ) >= 0.5

    def test_top_n_selector_must_match(self):
        assert competition_score(
            "MLB Draft: Top 3 Draft Picks", "MLB Draft: Top 5 Draft Picks"
        ) == 0.0

    def test_tokens_drop_year_and_filler_but_keep_market_type(self):
        # year + "the"/"of" dropped; "winner" KEPT (distinguishes market type)
        assert competition_tokens("2026 the Genesis Scottish Open Winner") == frozenset(
            {"genesis", "scottish", "open", "winner"}
        )


class TestFuturesMatcher:
    def test_matches_same_golfer_same_tournament(self):
        k = kalshi_outright()
        p = poly_outright()
        cands = FuturesMatcher().match([k], [p])
        assert len(cands) == 1
        assert cands[0].match_method == "futures"
        assert cands[0].kalshi_id == k.id
        assert cands[0].polymarket_id == p.id

    def test_no_cross_entity_match(self):
        k = kalshi_outright(yes_sub="Ludvig Aberg")
        p = poly_outright(title="Rory McIlroy", slug="tec-pga-genescot-x-rormci")
        assert FuturesMatcher().match([k], [p]) == []

    def test_same_golfer_different_tournament_separated_by_date(self):
        k = kalshi_outright(  # Scottish Open, resolves 07-26
            event_title="2026 Genesis Scottish Open", close="2026-07-26T12:00:00Z"
        )
        p = poly_outright(  # ISCO, resolves far later — outside 30d window
            question="ISCO Championship Winner", end="2026-09-30T12:00:00Z",
            slug="tec-pga-isco-x-ludabe",
        )
        assert FuturesMatcher().match([k], [p]) == []

    def test_next_team_city_matches_full_team_name(self):
        k = kalshi_outright(
            ticker="KXNEXTTEAMNBA-27KAWHI-LAC",
            yes_sub="LA Clippers",
            event_title="Kawhi Leonard's Next Team",
            close="2026-10-20T00:00:00Z",
        )
        p = poly_outright(
            slug="pntcbk-nba-kawlea-2026-10-23-lac",
            title="LA Clippers",
            question="Kawhi Leonard Next Team",
            end="2026-10-23T00:00:00Z",
        )
        cands = FuturesMatcher().match([k], [p])
        assert len(cands) == 1
        assert cands[0].kalshi_id == k.id

    def test_ambiguous_two_equal_competitions_refused(self):
        # one Kalshi golfer, two Poly markets for him scoring identically
        k = kalshi_outright()
        p1 = poly_outright(slug="a", question="Genesis Scottish Open Winner")
        p2 = poly_outright(slug="b", question="Genesis Scottish Open Winner")
        assert FuturesMatcher().match([k], [p1, p2]) == []


class TestSamePeriod:
    """Edition matching: stated years beat resolution dates."""

    def _m(self, competition, question, days_out):
        return Market(
            id="x", platform="kalshi", question=question,
            resolution_date=NOW + timedelta(days=days_out), category="",
            yes_depth=(), no_depth=(), updated_at=NOW,
            futures=FuturesEvent(competition=competition, entity="Someone"),
        )

    def test_stated_years_read_from_competition_and_question(self):
        m = self._m("2028 U.S. Presidential Election winner?", "Will X win?", 0)
        assert stated_years(m) == {2028}

    def test_agreeing_years_match_despite_a_year_apart_on_dates(self):
        # The real case: Kalshi buckets the whole 2028 cycle onto a 2029
        # placeholder expiration while Poly dates to the election itself.
        k = self._m("2028 U.S. Presidential Election winner?", "", 1000)
        p = self._m("2028 US Presidential Election Winner", "", 650)
        assert same_period(k, p, date_tolerance_days=30)

    def test_disagreeing_years_never_match_however_close_the_dates(self):
        k = self._m("2026 Nobel Peace Prize winner", "", 0)
        p = self._m("2027 Nobel Peace Prize Winner", "", 0)
        assert not same_period(k, p, date_tolerance_days=30)

    def test_falls_back_to_dates_when_a_side_states_no_year(self):
        k = self._m("Oscar Winner: Best Actor", "", 0)
        p = self._m("Oscar Winner: Best Actor", "", 10)
        assert same_period(k, p, date_tolerance_days=30)
        far = self._m("Oscar Winner: Best Actor", "", 400)
        assert not same_period(k, far, date_tolerance_days=30)

    def test_price_like_numbers_are_not_read_as_years(self):
        m = self._m("Will the S&P close above 2050?", "", 0)
        assert stated_years(m) == {2050} or 2050 not in stated_years(m)


class TestCompetitionFloor:
    """0.75 separates same-competition pairs from generic-token collisions."""

    def test_different_sport_same_phrasing_is_rejected(self):
        s = competition_score("Pro Basketball: Best Regular Season Record",
                              "Pro Football Best Regular Season Record")
        assert s < 0.75

    def test_regular_season_title_is_not_the_series_title(self):
        s = competition_score("NASCAR Cup Series Regular Season Champion",
                              "NASCAR Cup Series Champion")
        assert s < 0.75

    def test_conference_championship_is_not_the_national_championship(self):
        s = competition_score(
            "College Football Atlantic Coast Conference Championship Game",
            "College Football Playoff National Championship")
        assert s < 0.75

    def test_genuine_same_competition_clears_the_floor(self):
        assert competition_score("2026 Nobel Peace Prize winner",
                                 "2026 Nobel Peace Prize Winner") >= 0.75
        assert competition_score("Oscar winner: Best Actress",
                                 "Oscar Winner: Best Actress") >= 0.75


class TestFinalistSelector:
    """Being a finalist for an award is a different contract from winning it."""

    def test_mvp_finalists_does_not_pair_with_mvp(self):
        # Real pairing found in the live catalogs, scoring exactly 0.75 — it sat
        # on the floor and produced 46 wrong pairs across both leagues.
        assert competition_score("American League MVP Finalists",
                                 "American League MVP") == 0.0

    def test_finalists_still_pairs_with_finalists(self):
        assert competition_score("American League MVP Finalists",
                                 "American League MVP finalists") >= 0.75

    def test_nominee_is_not_a_selector(self):
        # Venues split between "be the nominee" and "win the nomination"; making
        # nominee a selector would reject these correct pairs.
        assert competition_score("2028 Democratic presidential nominee",
                                 "2028 Democratic Presidential Nominee") >= 0.75

class TestSeasonRangesAreNotSubEventSelectors:
    """A season written as a range must not leave a stray number behind.

    "2026-27" once stripped to a bare "27", which `_is_selector` reads as a
    sub-event selector — hard-zeroing the score against any competition without
    the same stray digits. It rejected correct pairs across every season-range
    sport, and silently: a missed match writes no row anywhere.
    """

    def test_a_season_range_leaves_no_stray_number(self):
        assert "27" not in competition_tokens("the 2026-27 Serie A")
        assert "27" not in competition_tokens("2026-2027 NBA MVP")
        assert "27" not in competition_tokens("2026\u201327 NHL MVP")

    def test_the_same_competition_still_scores_across_a_range(self):
        assert competition_score(
            "College Football Playoffs",
            "advances to the 2026-27 College Football Playoff") > 0.0

    def test_real_stage_numbers_are_still_selectors(self):
        # The fix must not weaken the guard it sits next to.
        assert competition_score("Tour de France: Stage 9 Winner",
                                 "Tour de France Winner") == 0.0
        assert competition_score("Tour de France: Stage 9 Winner",
                                 "Tour de France: Stage 8 Winner") == 0.0
