"""The hazard table's value is entirely in where it draws its lines.

A table keyed too coarsely (on the league) flags correct pairs and gets ignored;
a table keyed too narrowly measures nothing. So the tests that matter are the
calibration ones: they replay the 317 reviewed pairs and hold the table to the
boundary the review actually drew — ITF but not the main tours, fastest-lap but
not the race.
"""

import json
import pathlib

import pytest

from pmarb.matching.hazards import (
    HAZARDS,
    annotate,
    kalshi_series,
    settlement_hazards,
)
from pmarb.matching.matcher import MatchCandidate

REVIEWS = pathlib.Path(__file__).resolve().parent.parent / "reviews.json"


def _candidate(kalshi_id: str) -> MatchCandidate:
    return MatchCandidate(
        kalshi_id=kalshi_id, polymarket_id="polymarket_us:x",
        kalshi_question="q", polymarket_question="q",
        similarity_score=1.0, resolution_date_delta_days=0,
    )


class TestSeriesExtraction:
    def test_reads_the_series_off_a_game_ticker(self):
        assert kalshi_series(
            "kalshi:KXMLBGAME-26JUL081845HOUWSH-HOU") == "KXMLBGAME"

    def test_a_non_kalshi_id_has_no_series(self):
        # Callers pass either leg blindly; the Poly leg must not raise.
        assert kalshi_series("polymarket_us:aec-mlb-det-pit-2026-08-19") == ""
        assert settlement_hazards("polymarket_us:whatever") == ()

    def test_an_unlisted_series_is_empty_not_an_error(self):
        # Empty means "not in the table", NOT "no hazard exists".
        assert settlement_hazards("kalshi:KXMLBGAME-26JUL08-HOU") == ()


class TestAnnotation:
    def test_stamps_hazards_onto_candidates(self):
        out = annotate([_candidate("kalshi:KXITFMATCH-26AUG12-ABC"),
                        _candidate("kalshi:KXMLBGAME-26JUL08-HOU")])
        assert out[0].settlement_hazards == ("walkover_void_asymmetry",)
        assert out[1].settlement_hazards == ()

    def test_annotation_does_not_reject_anything(self):
        # A hazard says the hedge has a hole, not that the pair is wrong. The
        # count must survive: dropping flagged pairs here would silently shrink
        # the tracked universe and bias every rate computed from it.
        cands = [_candidate(f"kalshi:KXUFCFIGHT-{i}") for i in range(5)]
        assert len(annotate(cands)) == 5

    def test_candidates_are_replaced_not_mutated(self):
        c = _candidate("kalshi:KXNPBGAME-26AUG12-RAK")
        annotate([c])
        assert c.settlement_hazards == ()   # frozen dataclass, original intact


@pytest.mark.skipif(not REVIEWS.exists(), reason="no review labels on disk")
class TestCalibrationAgainstTheReviewedSet:
    """The table was DERIVED from these labels, so passing here is not evidence
    that it generalizes. What these tests do catch is the regression that
    matters: someone widening a row (to a league, a whole tour) and quietly
    flagging pairs the review said were fine."""

    @staticmethod
    def _load():
        rows = json.loads(REVIEWS.read_text())
        pos = [r for r in rows if r["resolution_match"] is True]
        neg = [r for r in rows if r["resolution_match"] is False]
        return pos, neg

    def test_flags_no_pair_the_review_called_a_true_match(self):
        pos, _ = self._load()
        flagged = [r for r in pos if settlement_hazards(r["kalshi_id"])]
        assert flagged == [], f"{len(flagged)} false flags on reviewed-true pairs"

    def test_catches_the_rules_divergence_negatives(self):
        # 19 of the 20 Class B negatives (the 28 others are matcher errors, a
        # different failure that hazards are not meant to catch).
        _, neg = self._load()
        caught = [r for r in neg if settlement_hazards(r["kalshi_id"])]
        assert len(caught) == 19

    def test_the_main_tours_are_deliberately_absent(self):
        # The whole reason this is keyed on series and not league: ATP/WTA
        # pairs were reviewed TRUE while ITF's bottom tiers were reviewed
        # FALSE. Adding "tennis" as a key would flag 8 correct pairs.
        for series in ("KXATP", "KXWTAMATCH", "KXATPCHALLENGERMATCH"):
            assert series not in HAZARDS

    def test_f1_race_markets_are_absent_only_fastest_lap_carries_it(self):
        # The reserve-driver clause applies to markets naming a DRIVER for a
        # lap-level outcome, not to race winner or constructor markets.
        assert "KXF1FASTLAP" in HAZARDS
        for series in ("KXF1", "KXF1CONSTRUCTORS", "KXF1RACE"):
            assert series not in HAZARDS


class TestEveryRowNamesItsMechanism:
    def test_hazard_names_are_from_the_known_set(self):
        # A free-text hazard string would make the field unqueryable; the
        # vocabulary stays closed until a review forces a new one.
        known = {"tie_possible", "walkover_void_asymmetry",
                 "substitute_reassignment"}
        for series, hazards in HAZARDS.items():
            assert hazards, f"{series} maps to no hazard"
            assert set(hazards) <= known, f"{series} carries an unknown hazard"
