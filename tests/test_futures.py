"""Futures matcher: entity-outright extraction + (competition, entity) matching.

Fixtures mirror real wire payloads (2026-07): Kalshi outright event/market
shapes and Polymarket US futures title/question fields.
"""

from datetime import datetime, timezone

from pmarb.feeds.kalshi import _market_metadata as kalshi_meta
from pmarb.feeds.polymarket import _market_metadata as poly_meta
from pmarb.matching.futures import (
    FuturesMatcher,
    competition_score,
    competition_tokens,
)

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


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
