"""Structured matcher for PLAYER PROPS.

The biggest matchable block on either venue, and the one that was hidden
longest: Kalshi's props phrase their subject as "Cal Raleigh: 2+", which reads
as an entity, so all 6,331 of them were classified as outrights and quietly
offered to the futures matcher. They never matched anything there — Poly's props
are not outright-shaped — so the block looked empty from both directions.

Identity is (game, player, stat, threshold), and all four are structured on
both venues:

    fact        Kalshi                        Polymarket US
    game        enclosing event title         description prose
    player      `yes_sub_title` before ":"    `title`
    stat        series ticker                 `sportsMarketType`
    threshold   "N+" (== floor_strike + .5)   `line`

THRESHOLD IS AN EQUALITY, not a tolerance: "2+ total bases" and "3+ total
bases" are different contracts on the same player in the same game, and the
prices differ precisely because the events do.

Unlike spreads, orientation is never in doubt — both venues write the same
"at least N" side as YES, and each feed refuses anything else — so these pairs
need no inversion. And unlike totals, no push exists: a player cannot record
14.5 receptions.

A match still means "same contract", NOT "same settlement rules". Poly's
touchdown props say "excluding passing touchdowns" in their own text; whether
Kalshi's touchdown series agrees is a resolution question, not a matching one.
"""

from __future__ import annotations

from collections import defaultdict

from pmarb.matching.matcher import MatchCandidate, _delta_days
from pmarb.matching.structured import competitor_score
from pmarb.models import Market

# Same window and reasoning as the game and line matchers: Kalshi's weekly
# tickers carry no time of day and parse to midnight Eastern.
START_TOLERANCE_HOURS = 30.0

_MIN_PLAYER_SCORE = 0.6   # "Kenneth Walker III" vs "Kenneth Walker"
_MIN_GAME_SCORE = 0.5     # product over both competitors
_TIE_MARGIN = 1e-9


def _same_game(kalshi: tuple[str, str], poly: tuple[str, str]) -> float:
    """How well two competitor pairs describe the same game (0 = they don't).

    The player and threshold already pin the contract down; this exists to stop
    the same player's props being crossed between two different games — a
    doubleheader, or consecutive days of the same series.
    """
    (k1, k2), (p1, p2) = kalshi, poly
    straight = competitor_score(k1, p1) * competitor_score(k2, p2)
    swapped = competitor_score(k1, p2) * competitor_score(k2, p1)
    return max(straight, swapped)


def _hours_apart(a, b) -> float | None:
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 3600.0


class PropMatcher:
    """Matches player props on (league, stat, threshold, player, game)."""

    def __init__(self, start_tolerance_hours: float = START_TOLERANCE_HOURS):
        self.start_tolerance_hours = start_tolerance_hours

    def match(
        self, kalshi_markets: list[Market], poly_markets: list[Market]
    ) -> list[MatchCandidate]:
        # Block on the exact triple; player and game are then verified within
        # the block. Threshold in the KEY is what keeps this cheap and is also
        # what makes it correct.
        blocks: dict[tuple, list[Market]] = defaultdict(list)
        for pm in poly_markets:
            if pm.prop is not None:
                pr = pm.prop
                blocks[(pr.league, pr.stat, pr.threshold)].append(pm)

        claims: list[tuple[float, float, Market, Market]] = []
        for km in kalshi_markets:
            kp = km.prop
            if kp is None:
                continue
            scored: list[tuple[float, float, Market]] = []
            for pm in blocks.get((kp.league, kp.stat, kp.threshold), ()):
                hours = _hours_apart(kp.start_time, pm.prop.start_time)
                if hours is None or hours > self.start_tolerance_hours:
                    continue
                player = competitor_score(kp.player, pm.prop.player)
                if player < _MIN_PLAYER_SCORE:
                    continue
                game = _same_game(kp.competitors, pm.prop.competitors)
                if game < _MIN_GAME_SCORE:
                    continue
                scored.append((player * game, hours, pm))
            if not scored:
                continue
            scored.sort(key=lambda s: (-s[0], s[1]))
            score, hours, pm = scored[0]
            if (len(scored) > 1
                    and abs(scored[1][0] - score) <= _TIE_MARGIN
                    and abs(scored[1][1] - hours) <= 1.0):
                continue  # two indistinguishable games — refuse to guess
            claims.append((score, hours, km, pm))

        # Strict 1:1 across the layer, best claim first — the same reason the
        # line matcher needs it: one player's 2+ hits prop exists on both days
        # of a series, and both fall inside the window.
        claims.sort(key=lambda c: (-c[0], c[1]))
        taken_k: set[str] = set()
        taken_p: set[str] = set()
        candidates: list[MatchCandidate] = []
        for score, _hours, km, pm in claims:
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
                match_method="prop",
            ))
        candidates.sort(key=lambda c: c.similarity_score, reverse=True)
        return candidates
