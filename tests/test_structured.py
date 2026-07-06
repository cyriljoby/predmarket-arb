"""Structured matcher: game-identity extraction + non-lexical pairing.

Fixtures mirror real wire payloads observed live (2026-07): Kalshi game-series
tickers/titles and Polymarket US moneyline marketSides.
"""

from datetime import datetime, timezone

from pmarb.feeds.kalshi import _market_metadata as kalshi_meta
from pmarb.feeds.kalshi import _parse_event_start
from pmarb.feeds.polymarket import _market_metadata as poly_meta
from pmarb.matching.structured import StructuredMatcher, competitor_score

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def kalshi_game_market(
    ticker="KXMLBGAME-26JUL081845HOUWSH-HOU",
    yes_sub="Houston",
    event_title="Houston vs Washington",
    close="2026-07-11T22:45:00Z",
):
    market = {
        "ticker": ticker,
        "title": f"{event_title} Winner?",
        "yes_sub_title": yes_sub,
        "close_time": close,
        "category": "Sports",
    }
    return kalshi_meta(market, NOW, {"title": event_title})


def poly_moneyline(
    slug="aec-mlb-hou-was-2026-07-08",
    teams=(("Houston Astros", "hou", True), ("Washington Nationals", "was", False)),
    league="mlb",
    game_start="2026-07-08T22:45:00Z",
    end="2026-07-22T00:00:00Z",
):
    market = {
        "slug": slug,
        "question": f"{teams[0][0]} vs. {teams[1][0]}",
        "marketType": "moneyline",
        "gameStartTime": game_start,
        "endDate": end,
        "category": "sports",
        "marketSides": [
            {
                "long": is_long,
                "description": name,
                "team": {"name": name, "abbreviation": abbr, "league": league},
            }
            for name, abbr, is_long in teams
        ],
    }
    return poly_meta(market, NOW)


class TestKalshiEventExtraction:
    def test_parses_game_identity(self):
        m = kalshi_game_market()
        assert m.event is not None
        assert m.event.league == "mlb"
        assert m.event.competitors == ("Houston", "Washington")
        assert m.event.yes_competitor == "Houston"
        assert m.event.yes_abbrev == "hou"

    def test_start_time_is_eastern_converted_to_utc(self):
        # 26JUL081845 = 2026-07-08 18:45 ET = 22:45 UTC (EDT)
        start = _parse_event_start("26JUL081845HOUWSH")
        assert start == datetime(2026, 7, 8, 22, 45, tzinfo=timezone.utc)

    def test_date_only_segment_parses_to_midnight_eastern(self):
        start = _parse_event_start("26SEP14DALSEA")
        assert start == datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)

    def test_non_game_series_has_no_event(self):
        m = kalshi_meta(
            {"ticker": "KXFEDDECISION-26JUL-HIKE", "title": "Rate hike?",
             "yes_sub_title": "Hike", "close_time": "2026-07-29T18:00:00Z"},
            NOW,
            {"title": "Fed decision in July"},
        )
        assert m.event is None

    def test_title_decoration_is_stripped(self):
        m = kalshi_game_market(
            ticker="KXUFCFIGHT-26JUL11MCGHOL-MCG",
            yes_sub="Conor McGregor",
            event_title="McGregor vs. Holloway 2",
        )
        assert m.event.competitors == ("McGregor", "Holloway")
        assert m.event.league == "ufc"


class TestPolyEventExtraction:
    def test_parses_moneyline_sides(self):
        m = poly_moneyline()
        assert m.event is not None
        assert m.event.league == "mlb"
        assert m.event.yes_competitor == "Houston Astros"
        assert m.event.yes_abbrev == "hou"
        assert m.event.start_time == datetime(2026, 7, 8, 22, 45, tzinfo=timezone.utc)

    def test_question_synthesized_with_yes_semantics(self):
        m = poly_moneyline()
        assert m.question == (
            "Will Houston Astros win Houston Astros vs. Washington Nationals?"
        )

    def test_non_moneyline_has_no_event(self):
        m = poly_meta(
            {"slug": "tec-pga-x", "question": "Will X win the Open",
             "marketType": "futures", "endDate": "2026-07-20T00:00:00Z"},
            NOW,
        )
        assert m.event is None


class TestCompetitorScore:
    def test_exact(self):
        assert competitor_score("Jessica Pegula", "Jessica Pegula") == 1.0

    def test_city_subset_of_full_team_name(self):
        assert competitor_score("Houston", "Houston Astros") > 0.6

    def test_surname_subset_of_full_name(self):
        assert competitor_score("Pegula", "Jessica Pegula") > 0.6

    def test_unrelated_is_zero(self):
        assert competitor_score("Houston Astros", "Seattle Mariners") == 0.0

    def test_accent_folding(self):
        assert competitor_score("Bjorn Borg", "Björn Borg") == 1.0


class TestStructuredMatcher:
    def test_matches_game_and_aligns_yes_side(self):
        k = kalshi_game_market()  # YES = Houston
        p = poly_moneyline()      # long = Houston Astros
        cands = StructuredMatcher().match([k], [p])
        assert len(cands) == 1
        assert cands[0].kalshi_id == k.id
        assert cands[0].polymarket_id == p.id
        assert cands[0].match_method == "structured"

    def test_picks_kalshi_market_for_the_long_side(self):
        k_hou = kalshi_game_market()
        k_was = kalshi_game_market(
            ticker="KXMLBGAME-26JUL081845HOUWSH-WSH", yes_sub="Washington"
        )
        p = poly_moneyline(
            teams=(("Washington Nationals", "was", True),
                   ("Houston Astros", "hou", False)),
        )
        cands = StructuredMatcher().match([k_hou, k_was], [p])
        assert len(cands) == 1
        assert cands[0].kalshi_id == k_was.id  # YES sides agree: Washington

    def test_no_match_across_leagues(self):
        k = kalshi_game_market()
        p = poly_moneyline(league="npb")
        assert StructuredMatcher().match([k], [p]) == []

    def test_no_match_outside_start_window(self):
        k = kalshi_game_market()
        p = poly_moneyline(game_start="2026-07-12T22:45:00Z")
        assert StructuredMatcher().match([k], [p]) == []

    def test_doubleheader_resolved_by_closest_start(self):
        early = kalshi_game_market(
            ticker="KXMLBGAME-26JUL081305HOUWSH-HOU"  # 13:05 ET = 17:05 UTC
        )
        late = kalshi_game_market()  # 18:45 ET = 22:45 UTC
        p = poly_moneyline(game_start="2026-07-08T22:45:00Z")
        cands = StructuredMatcher().match([early, late], [p])
        assert len(cands) == 1
        assert cands[0].kalshi_id == late.id

    def test_derby_disambiguated_by_matching_abbrevs(self):
        # "New York Y" / "New York M" tokenize identically once single-char
        # disambiguators drop — but the venues' shared team code (NYY == nyy)
        # breaks the tie, so this matches, and to the right side.
        k = kalshi_game_market(
            ticker="KXMLBGAME-26JUL081845NYYNYM-NYY",
            yes_sub="New York Y",
            event_title="New York Y vs New York M",
        )
        p = poly_moneyline(
            slug="aec-mlb-nyy-nym-2026-07-08",
            teams=(("New York Yankees", "nyy", True),
                   ("New York Mets", "nym", False)),
        )
        cands = StructuredMatcher().match([k], [p])
        assert len(cands) == 1
        assert cands[0].kalshi_id == k.id

    def test_ambiguous_derby_without_abbrev_agreement_is_refused(self):
        # Same derby, but the venues use different team codes (NYA vs nyy):
        # no boost, both assignments tie, and the matcher refuses to guess —
        # guessing wrong would invert the hedge.
        k = kalshi_game_market(
            ticker="KXMLBGAME-26JUL081845NYANYM-NYA",
            yes_sub="New York Y",
            event_title="New York Y vs New York M",
        )
        p = poly_moneyline(
            slug="aec-mlb-nyy-nym-2026-07-08",
            teams=(("New York Yankees", "nyy", True),
                   ("New York Mets", "nym", False)),
        )
        assert StructuredMatcher().match([k], [p]) == []

    def test_tennis_surname_vs_full_name(self):
        k = kalshi_game_market(
            ticker="KXWTAMATCH-26JUL07PEGGAU-PEG",
            yes_sub="Jessica Pegula",
            event_title="Pegula vs Gauff",
        )
        p = poly_moneyline(
            slug="aec-wta-jespeg-cocgau-2026-07-07",
            teams=(("Jessica Pegula", "jespeg", True),
                   ("Coco Gauff", "cocgau", False)),
            league="wta",
            game_start="2026-07-07T15:00:00Z",
        )
        cands = StructuredMatcher().match([k], [p])
        assert len(cands) == 1
        assert cands[0].kalshi_id == k.id
