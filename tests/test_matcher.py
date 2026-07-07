"""Unit tests for the rule-based matcher."""

import json
from datetime import datetime, timedelta, timezone

from pmarb.matching.matcher import (
    MatchCandidate,
    RuleBasedMatcher,
    jaccard,
    similarity,
    tokenize,
    write_matches,
)
from pmarb.models import Market

DAY = datetime(2026, 9, 1, tzinfo=timezone.utc)


def mkt(platform, mid, question, date=DAY):
    return Market(
        id=f"{platform}:{mid}", platform=platform, question=question,
        resolution_date=date, category="", yes_depth=(), no_depth=(), updated_at=DAY,
    )


class TestTokenize:
    def test_strips_stopwords_and_punctuation(self):
        assert tokenize("Will the Chargers beat the Titans?") == {"chargers", "titans"}

    def test_keeps_numbers(self):
        assert "2026" in tokenize("Fed funds rate in 2026")


class TestSimilarity:
    def test_jaccard(self):
        assert jaccard({"a", "b"}, {"b", "c"}) == 1 / 3

    def test_identical_is_high(self):
        q = "Will the Chargers win the Super Bowl?"
        assert similarity(tokenize(q), tokenize(q), q, q) > 0.95

    def test_unrelated_is_low(self):
        a, b = "Will it rain in Seattle?", "Fed raises rates in March"
        assert similarity(tokenize(a), tokenize(b), a, b) < 0.4


class TestMatch:
    def test_matches_near_identical_questions(self):
        k = [mkt("kalshi", "K1", "Will the Chargers beat the Titans?")]
        p = [
            mkt("polymarket_us", "P1", "Will the Chargers beat the Titans?"),
            mkt("polymarket_us", "P2", "Will it rain in Seattle tomorrow?"),
        ]
        out = RuleBasedMatcher().match(k, p)
        assert len(out) == 1
        assert out[0].kalshi_id == "kalshi:K1"
        assert out[0].polymarket_id == "polymarket_us:P1"
        assert out[0].resolution_match is None  # unreviewed by default

    def test_date_tolerance_filters_far_dates(self):
        k = [mkt("kalshi", "K1", "Will the Chargers beat the Titans?")]
        p = [mkt("polymarket_us", "P1", "Will the Chargers beat the Titans?",
                 date=DAY + timedelta(days=30))]
        assert RuleBasedMatcher().match(k, p) == []

    def test_threshold_filters_dissimilar(self):
        k = [mkt("kalshi", "K1", "Will the Chargers beat the Titans?")]
        p = [mkt("polymarket_us", "P1", "Will inflation exceed 4 percent?")]
        assert RuleBasedMatcher().match(k, p) == []

    def test_blocking_skips_markets_with_no_shared_tokens(self):
        # An unrelated kalshi market shares no tokens -> never even scored.
        k = [mkt("kalshi", "K1", "Will SpaceX reach Mars?")]
        p = [mkt("polymarket_us", "P1", "Will the Chargers beat the Titans?")]
        assert RuleBasedMatcher().match(k, p) == []

    def test_custom_threshold(self):
        k = [mkt("kalshi", "K1", "Chargers beat Titans Sunday")]
        p = [mkt("polymarket_us", "P1", "Will the Chargers beat the Titans?")]
        assert RuleBasedMatcher(threshold=0.99).match(k, p) == []
        assert len(RuleBasedMatcher(threshold=0.4).match(k, p)) == 1


class TestMultiOutcomeGuard:
    def _structured(self, platform, mid, q):
        from pmarb.models import FuturesEvent
        return Market(
            id=f"{platform}:{mid}", platform=platform, question=q,
            resolution_date=DAY, category="", yes_depth=(), no_depth=(),
            updated_at=DAY,
            futures=FuturesEvent(entity="X", competition="C"),
        )

    def test_skips_markets_with_structured_identity(self):
        # Identical text, but both carry structured identity -> lexical refuses
        # (they belong to the futures/structured tiers, not fuzzy text).
        k = [self._structured("kalshi", "K1", "Will the Chargers beat the Titans?")]
        p = [self._structured("polymarket_us", "P1", "Will the Chargers beat the Titans?")]
        assert RuleBasedMatcher().match(k, p) == []

    def test_still_matches_unstructured(self):
        k = [mkt("kalshi", "K1", "Will the Chargers beat the Titans?")]
        p = [mkt("polymarket_us", "P1", "Will the Chargers beat the Titans?")]
        assert len(RuleBasedMatcher().match(k, p)) == 1

    def test_skips_pair_if_either_side_structured(self):
        k = [self._structured("kalshi", "K1", "Will the Chargers beat the Titans?")]
        p = [mkt("polymarket_us", "P1", "Will the Chargers beat the Titans?")]
        assert RuleBasedMatcher().match(k, p) == []

    def test_guard_can_be_disabled(self):
        k = [self._structured("kalshi", "K1", "Will the Chargers beat the Titans?")]
        p = [self._structured("polymarket_us", "P1", "Will the Chargers beat the Titans?")]
        assert len(RuleBasedMatcher().match(k, p, skip_structured=False)) == 1


class TestWriteMatches:
    def test_writes_expected_json(self, tmp_path):
        c = MatchCandidate(
            kalshi_id="kalshi:K1", polymarket_id="polymarket_us:P1",
            kalshi_question="q", polymarket_question="q",
            similarity_score=0.88, resolution_date_delta_days=1,
        )
        path = tmp_path / "matches.json"
        write_matches([c], str(path))
        data = json.loads(path.read_text())
        assert data[0]["kalshi_id"] == "kalshi:K1"
        assert data[0]["resolution_match"] is None
        assert data[0]["similarity_score"] == 0.88
