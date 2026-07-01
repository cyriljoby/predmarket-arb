"""Rule-based market matcher with token blocking.

The naive approach — every Kalshi market vs every Polymarket market — is ~574M
comparisons. So we *block*: build an inverted index of question tokens, and only
score pairs that share a meaningful word. That turns hundreds of millions of
comparisons into a few thousand.

Scoring blends Jaccard token overlap with a character-level ratio. Pairs above
`MATCH_THRESHOLD` whose resolution dates fall within `RESOLUTION_DATE_TOLERANCE_DAYS`
become candidates — written to `matches.json` for MANUAL resolution-criteria
review. A high score means the questions *look* alike; it does NOT mean they
resolve identically. That judgement (`resolution_match`) is a human's.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

from pmarb.config import (
    MATCH_LOG_PATH,
    MATCH_THRESHOLD,
    RESOLUTION_DATE_TOLERANCE_DAYS,
)
from pmarb.models import Market

# Function words that carry no matching signal (and would create huge blocks).
_STOPWORDS = frozenset(
    "will the a an be to of in on by at is are was were for and or not this that "
    "it as with than then who what when which over under above below before after "
    "win wins beat".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> set[str]:
    """Lowercase word/number tokens, minus stopwords and single chars."""
    return {
        t for t in _TOKEN_RE.findall(text.lower())
        if len(t) > 1 and t not in _STOPWORDS
    }


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def similarity(
    a_tokens: set[str], b_tokens: set[str], a_text: str, b_text: str
) -> float:
    """Blend token overlap (Jaccard) with character-level ratio, equally."""
    ratio = SequenceMatcher(None, a_text.lower(), b_text.lower()).ratio()
    return 0.5 * jaccard(a_tokens, b_tokens) + 0.5 * ratio


def _delta_days(a: Market, b: Market) -> int | None:
    if a.resolution_date is None or b.resolution_date is None:
        return None
    return abs((a.resolution_date - b.resolution_date).days)


@dataclass(frozen=True)
class MatchCandidate:
    kalshi_id: str
    polymarket_id: str
    kalshi_question: str
    polymarket_question: str
    similarity_score: float
    resolution_date_delta_days: int
    resolution_match: bool | None = None  # set MANUALLY during review
    resolution_notes: str = ""
    verified_by: str | None = None


class RuleBasedMatcher:
    """Phase 1 matcher. `match()` is the seam Phase 2 will swap for embeddings."""

    def __init__(
        self,
        threshold: float = MATCH_THRESHOLD,
        date_tolerance_days: int = RESOLUTION_DATE_TOLERANCE_DAYS,
        max_block_df: int = 2000,
    ):
        self.threshold = threshold
        self.date_tolerance_days = date_tolerance_days
        # Tokens appearing in more than this many markets are too common to block
        # on (they'd pull in everything) — skipped for candidate generation.
        self.max_block_df = max_block_df

    def match(
        self, kalshi_markets: list[Market], poly_markets: list[Market]
    ) -> list[MatchCandidate]:
        # Each poly market has one or more text representations (question + any
        # aliases); we tokenize each and score against all of them, taking the
        # best. This lets futures markets match on their description-derived
        # question while game markets match on their concise raw question.
        poly_reps: list[list[tuple[str, set[str]]]] = [
            [(t, tokenize(t)) for t in (m.question, *m.match_aliases) if t]
            for m in poly_markets
        ]

        index: dict[str, set[int]] = defaultdict(set)
        for i, reps in enumerate(poly_reps):
            for _, toks in reps:
                for t in toks:
                    index[t].add(i)
        too_common = {t for t, idxs in index.items() if len(idxs) > self.max_block_df}

        # similarity = 0.5*jaccard + 0.5*ratio, ratio <= 1, so a pair can only
        # reach `threshold` if jaccard >= 2*threshold - 1. Below that we can skip
        # the expensive character ratio entirely (no false negatives).
        min_jaccard = max(0.0, 2 * self.threshold - 1)

        candidates: list[MatchCandidate] = []
        for km in kalshi_markets:
            k_tokens = tokenize(km.question)
            # Blocking: only poly markets that share a (non-common) token.
            block: set[int] = set()
            for t in k_tokens:
                if t not in too_common:
                    block.update(index.get(t, ()))

            for j in block:
                pm = poly_markets[j]
                delta = _delta_days(km, pm)
                if delta is None or delta > self.date_tolerance_days:
                    continue
                # Best score across this poly market's text representations.
                best = 0.0
                best_text = pm.question
                for text, toks in poly_reps[j]:
                    if jaccard(k_tokens, toks) < min_jaccard:
                        continue  # provably cannot reach threshold — skip the ratio
                    score = similarity(k_tokens, toks, km.question, text)
                    if score > best:
                        best, best_text = score, text
                if best >= self.threshold:
                    candidates.append(
                        MatchCandidate(
                            kalshi_id=km.id,
                            polymarket_id=pm.id,
                            kalshi_question=km.question,
                            polymarket_question=best_text,
                            similarity_score=round(best, 4),
                            resolution_date_delta_days=delta,
                        )
                    )
        candidates.sort(key=lambda c: c.similarity_score, reverse=True)
        return candidates


def write_matches(
    candidates: list[MatchCandidate], path: str = MATCH_LOG_PATH
) -> None:
    """Write candidates to disk as pretty JSON for manual review."""
    with open(path, "w") as f:
        json.dump([asdict(c) for c in candidates], f, indent=2)
