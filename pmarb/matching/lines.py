"""Structured matcher for SPREAD and TOTAL markets.

Spreads and totals are the largest unmatched block in both catalogs — 85% of
Poly US's listings are props/spreads/totals, and Kalshi runs ~5,000 full-game
line markets that no matcher touched. They are also, unlike props, exactly
matchable: both venues publish the line as a NUMBER (Kalshi `floor_strike`,
Poly `line`), so identity is arithmetic rather than text similarity.

Match key: same league, same kind, SAME LINE, competitors aligned, game start
within tolerance. The line is an equality, never a tolerance — a total at 45.5
and one at 46.5 on the same game are different contracts, and treating them as
one manufactures an edge out of two unrelated bets.

ORIENTATION is what makes this more than a copy of the game matcher. Kalshi
writes one market per side ("Vanderbilt wins by over 41.5"). Poly writes ONE
market per (game, line), and its long side may be either the favorite (-41.5)
or the underdog (+41.5): measured on the live catalog, 4,046 (game, line)
groups were favorite-side and 4,477 underdog-side, and NEVER both. So on
roughly half the inventory Poly's YES is the COMPLEMENT of Kalshi's YES.

That is not a detail. The detector hedges by buying Kalshi YES against Poly NO;
if the two YESes are already complements, that pairing buys the SAME event
twice while reporting a hedge. So each candidate records `poly_inverted`, and
the collector swaps the Poly leg's ladders before evaluating. A pair whose
orientation cannot be established is refused, not guessed at.
"""

from __future__ import annotations

from collections import defaultdict

from pmarb.matching.matcher import MatchCandidate, _delta_days
from pmarb.matching.structured import competitor_score
from pmarb.models import Market

# Both legs must agree on which game this is. Kalshi's weekly-sport tickers
# carry no time of day and parse to midnight Eastern, so the window has to
# absorb a whole day; the same 30h the game matcher uses, for the same reason.
START_TOLERANCE_HOURS = 30.0

_MIN_PAIR_SCORE = 0.5   # product of both competitor scores
_MIN_SIDE_SCORE = 0.6   # and each side individually
_TIE_MARGIN = 1e-9


def _align(
    kalshi: tuple[str, str], poly: tuple[str, str]
) -> dict[str, str] | None:
    """Map each Poly competitor to its Kalshi counterpart, or None.

    Same shape as the game matcher's alignment: score the straight and swapped
    assignments, take the better, and refuse a tie — with two competitors,
    guessing wrong is not a near miss, it inverts the hedge.
    """
    (k1, k2), (p1, p2) = kalshi, poly
    straight = (competitor_score(k1, p1), competitor_score(k2, p2))
    swapped = (competitor_score(k1, p2), competitor_score(k2, p1))
    s_score, w_score = straight[0] * straight[1], swapped[0] * swapped[1]
    if abs(s_score - w_score) <= _TIE_MARGIN:
        return None
    best, sides, mapping = (
        (s_score, straight, {p1: k1, p2: k2}) if s_score > w_score
        else (w_score, swapped, {p2: k1, p1: k2})
    )
    if best < _MIN_PAIR_SCORE or min(sides) < _MIN_SIDE_SCORE:
        return None
    return mapping


def _hours_apart(a, b) -> float | None:
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 3600.0


def _orientation(km: Market, pm: Market, mapping: dict[str, str]) -> bool | None:
    """Is the Poly leg's YES the complement of the Kalshi leg's YES?

    False = the two YESes name the same event (pair like a moneyline).
    True  = they are complements; the Poly ladders must be swapped.
    None  = undecidable, so the pair is dropped.

    Totals are always direct: Kalshi writes "Over N", and Poly's long side was
    Over on every one of the 9,672 markets sampled — the feed refuses any
    other shape, so reaching here means both are Over.
    """
    if km.line.kind == "total":
        return False
    # Spread. Kalshi always writes the LAYING side: "X wins by over L", i.e.
    # X at -L. Poly writes one side of one line, and the sign decides which
    # Kalshi market — if either — it may legally pair with:
    #
    #   Poly YES = T at -L  ("T wins by more than L")
    #       identical to Kalshi's market for T. Direct.
    #   Poly YES = T at +L  ("T loses by less than L, or wins")
    #       the exact complement of Kalshi's market for T's OPPONENT
    #       ("opponent wins by over L"). Inverted.
    #
    # Any other combination is neither equal nor complementary — notably
    # Poly "T +L" against Kalshi "T wins by over L", which shares a team and a
    # number while describing two different bets. Those must be refused: a
    # matched pair that is not a hedge is the failure this whole layer exists
    # to prevent.
    poly_yes_as_kalshi = mapping.get(pm.line.yes_team)
    if poly_yes_as_kalshi is None:
        return None
    same_team = (
        competitor_score(poly_yes_as_kalshi, km.line.yes_team) >= _MIN_SIDE_SCORE
    )
    if pm.line.yes_favored:
        return False if same_team else None
    return None if same_team else True


class LineMatcher:
    """Matches spread/total markets on (league, kind, line, game)."""

    def __init__(self, start_tolerance_hours: float = START_TOLERANCE_HOURS):
        self.start_tolerance_hours = start_tolerance_hours

    def match(
        self, kalshi_markets: list[Market], poly_markets: list[Market]
    ) -> list[MatchCandidate]:
        # Block on the exact triple. The line being part of the KEY rather than
        # a scored feature is what keeps this cheap: 19k Poly line markets are
        # never compared against 5k Kalshi ones, only within a shared line.
        blocks: dict[tuple, list[Market]] = defaultdict(list)
        for pm in poly_markets:
            if pm.line is not None:
                blocks[(pm.line.league, pm.line.kind, pm.line.line)].append(pm)

        claims: list[tuple[float, float, Market, Market, bool]] = []
        for km in kalshi_markets:
            kl = km.line
            if kl is None:
                continue
            scored: list[tuple[float, float, Market, bool]] = []
            for pm in blocks.get((kl.league, kl.kind, kl.line), ()):
                hours = _hours_apart(kl.start_time, pm.line.start_time)
                if hours is None or hours > self.start_tolerance_hours:
                    continue
                mapping = _align(kl.competitors, pm.line.competitors)
                if mapping is None:
                    continue
                inverted = _orientation(km, pm, mapping)
                if inverted is None:
                    continue  # orientation unknown -> not a hedge, drop it
                score = min(
                    competitor_score(v, k) for k, v in mapping.items()
                )
                scored.append((score, hours, pm, inverted))
            if not scored:
                continue
            # Same teams, same line, twice in the window (a doubleheader, or a
            # rescheduled game): best alignment wins, closest start breaks it.
            scored.sort(key=lambda s: (-s[0], s[1]))
            score, hours, pm, inverted = scored[0]
            if (len(scored) > 1
                    and abs(scored[1][0] - score) <= _TIE_MARGIN
                    and abs(scored[1][1] - hours) <= 1.0):
                continue  # indistinguishable candidates — refuse to guess
            claims.append((score, hours, km, pm, inverted))

        # Resolve to a strict 1:1 across the whole layer, best claim first.
        #
        # The per-market loop above only stops ONE Kalshi market from taking two
        # Poly markets. The opposite collision is just as wrong and happens for
        # real: Arizona at Houston on consecutive days is the same teams at the
        # same line twice, and both Kalshi games fall inside the 30h window, so
        # both claimed the same Poly market. Closest start decides, which is
        # what pairs each game with its own listing.
        claims.sort(key=lambda c: (-c[0], c[1]))
        taken_k: set[str] = set()
        taken_p: set[str] = set()
        candidates: list[MatchCandidate] = []
        for score, _hours, km, pm, inverted in claims:
            if km.id in taken_k or pm.id in taken_p:
                continue
            taken_k.add(km.id)
            taken_p.add(pm.id)
            candidates.append(MatchCandidate(
                kalshi_id=km.id,
                polymarket_id=pm.id,
                kalshi_question=km.question,
                polymarket_question=pm.question,
                similarity_score=round(score, 4),
                resolution_date_delta_days=_delta_days(km, pm) or 0,
                match_method="line",
                poly_inverted=inverted,
            ))
        candidates.sort(key=lambda c: c.similarity_score, reverse=True)
        return candidates
