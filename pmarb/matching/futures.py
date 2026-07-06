"""Structured matcher for entity-outright markets (futures).

Lexical similarity over-matches outrights: a golf field is ~150 near-identical
"Will {golfer} win {tournament}" questions, so string matching produces a
many-to-many mess and the multi-outcome false positives the detector flagged
(the Kawhi "next team" +95% phantom). But both venues encode a clean
`(competition, entity)` identity (see `FuturesEvent`), so this matcher grounds
each market in that pair and matches one-to-one.

Match key: same ENTITY (golfer/team/person — reuses `competitor_score`'s
name-subset logic) in the same COMPETITION (token overlap on the normalized
question), disambiguated by resolution date (the year is dropped from the
competition text, so 2026 vs 2027 Scottish Open separate only by date).

Like the game matcher, a match means "same outright, same subject", NOT "same
resolution rules" — void/tie/withdrawal handling still diverges, so
`resolution_match` stays a human/LLM judgement.
"""

from __future__ import annotations

import re
from collections import defaultdict

from pmarb.config import (
    FUTURES_COMPETITION_MIN,
    FUTURES_DATE_TOLERANCE_DAYS,
    FUTURES_ENTITY_MIN,
)
from pmarb.matching.matcher import MatchCandidate, _delta_days
from pmarb.matching.structured import competitor_score
from pmarb.models import Market

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Pure grammatical filler only. The year is dropped (Poly omits it from the
# question, Kalshi includes it); resolution-date proximity separates seasons.
_COMP_STOP = frozenset("the a an of to be who will in".split())
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_TIE_MARGIN = 1e-9

# Sub-event selectors: words that pick out ONE contract within a competition
# (Stage 9, Round 1, Top 3). Any pure number is also a selector. These must
# match EXACTLY between two competitions — a "Stage 9" market may only pair
# with a "Stage 9" market, never with the overall "Tour de France Winner".
# Synonym-y type words (winner/champion/finals/leader) are deliberately NOT
# selectors — venues phrase those inconsistently; they're scored as name tokens.
_SELECTOR_KEYWORDS = frozenset(
    "stage round leg heat group top matchday game".split()
)


def _is_selector(token: str) -> bool:
    return token.isdigit() or token in _SELECTOR_KEYWORDS


def competition_tokens(text: str) -> frozenset[str]:
    """Normalized token set of a competition string, year and filler removed.

    Numbers are KEPT (a stage/round number is load-bearing); only single-letter
    tokens and grammatical filler are dropped.
    """
    text = _YEAR_RE.sub(" ", text.lower())
    return frozenset(
        t for t in _TOKEN_RE.findall(text)
        if (len(t) > 1 or t.isdigit()) and t not in _COMP_STOP
    )


def competition_score(a: str, b: str) -> float:
    """Match score for two competitions.

    Hard gate: the sub-event selector sets (stage/round/top + numbers) must be
    identical, else the two name the same competition but different contracts
    (stage-9 vs overall, round-1-leader vs winner) — score 0. Otherwise Jaccard
    over the remaining name tokens.
    """
    ta, tb = competition_tokens(a), competition_tokens(b)
    if not ta or not tb:
        return 0.0
    sel_a = {t for t in ta if _is_selector(t)}
    sel_b = {t for t in tb if _is_selector(t)}
    if sel_a != sel_b:
        return 0.0
    name_a, name_b = ta - sel_a, tb - sel_b
    if not name_a or not name_b:
        return 0.0
    return len(name_a & name_b) / len(name_a | name_b)


class FuturesMatcher:
    """Matches entity-outright markets on (competition, entity), not raw text."""

    def __init__(
        self,
        entity_min: float = FUTURES_ENTITY_MIN,
        competition_min: float = FUTURES_COMPETITION_MIN,
        date_tolerance_days: int = FUTURES_DATE_TOLERANCE_DAYS,
    ):
        self.entity_min = entity_min
        self.competition_min = competition_min
        self.date_tolerance_days = date_tolerance_days

    def match(
        self, kalshi_markets: list[Market], poly_markets: list[Market]
    ) -> list[MatchCandidate]:
        polys = [m for m in poly_markets if m.futures is not None]

        # Block by entity token: only compare markets that share an entity word,
        # so a golfer is scored against the same golfer, not the whole field.
        index: dict[str, list[int]] = defaultdict(list)
        for j, pm in enumerate(polys):
            for tok in _entity_tokens(pm.futures.entity):
                index[tok].append(j)

        candidates: list[MatchCandidate] = []
        for km in kalshi_markets:
            kf = km.futures
            if kf is None:
                continue
            block = {j for tok in _entity_tokens(kf.entity) for j in index.get(tok, ())}

            scored: list[tuple[float, Market]] = []
            for j in block:
                pm = polys[j]
                delta = _delta_days(km, pm)
                if delta is None or delta > self.date_tolerance_days:
                    continue
                e = competitor_score(kf.entity, pm.futures.entity)
                if e < self.entity_min:
                    continue
                c = competition_score(kf.competition, pm.futures.competition)
                if c < self.competition_min:
                    continue
                scored.append((e * c, pm))

            if not scored:
                continue
            scored.sort(key=lambda s: -s[0])
            score, pm = scored[0]
            # Two equally-good competitions for the same entity (e.g. a "winner"
            # and a "top 5" market that both cleared the bar) — refuse to guess.
            if len(scored) > 1 and abs(scored[1][0] - score) <= _TIE_MARGIN:
                continue
            candidates.append(MatchCandidate(
                kalshi_id=km.id,
                polymarket_id=pm.id,
                kalshi_question=km.question,
                polymarket_question=pm.question,
                similarity_score=round(score, 4),
                resolution_date_delta_days=_delta_days(km, pm) or 0,
                match_method="futures",
            ))
        candidates.sort(key=lambda c: c.similarity_score, reverse=True)
        return candidates


def _entity_tokens(name: str) -> frozenset[str]:
    """Entity blocking tokens — lowercased words >1 char (drops initials)."""
    return frozenset(t for t in _TOKEN_RE.findall(name.lower()) if len(t) > 1)
