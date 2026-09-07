"""Structured (non-lexical) matcher for head-to-head sports/esports markets.

The lexical matcher can't touch moneylines: Kalshi phrases them per-team
("Will Houston win the Houston vs Washington Winner?") while Polymarket US's
question is just "Team A vs. Team B" with no YES semantics. But both venues
fully encode the game in metadata — league, start time, both competitors, and
which competitor YES pays on (see `SportsEvent` in models). This matcher keys
on that identity and never looks at question text.

Pipeline:
  1. Group each venue's SportsEvent markets into games (Kalshi: one market per
     competitor grouped by event ticker; Poly: one market per game).
  2. Block by league; within a block, compare games whose start times fall
     within `start_tolerance_hours`.
  3. Align competitors by normalized-name subset matching ("Houston" ⊆
     "Houston Astros", "Pegula" ⊆ "Jessica Pegula"), with an exact
     venue-abbreviation match as a confirming boost. Both competitors must
     align consistently — a game-level check, so "New York" alone can't pair
     the Yankees with the Mets unless the opponents also collide.
  4. Emit ONE candidate per game: the Kalshi market whose YES competitor is
     the Poly market's YES (long) competitor. detect_pair walks both books'
     yes/no depth, so the single aligned pair covers both arb directions.

The resolution-date tolerance is deliberately NOT applied here: game markets
pad settlement dates differently per venue (Kalshi ~+3d, Poly US ~+14d).
Game start time is the real identity key; the actual date delta is still
recorded on the candidate.

Same caveat as the lexical matcher: a match means "same game, same side",
NOT "same resolution rules". Draws, postponements, and regulation-vs-full-time
rules still diverge (`resolution_match` stays a human/LLM judgement).
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from pmarb.matching.matcher import MatchCandidate, _delta_days
from pmarb.models import Market

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Tokens that DISTINGUISH two otherwise-identical programs rather than decorate
# one. "Kansas" and "Kansas St." are different schools; "Houston" and "Houston
# Astros" are the same club. Both look like a subset to the token rule, so the
# subset shortcut is refused whenever one side carries one of these and the
# other does not. Learned live: KXNCAAFPLAYOFF-26-KSU (Kansas State) matched
# Poly's Kansas market and became the highest-edge "arb" in the set — two
# independent bets, not a hedge.
_DISCRIMINATORS = frozenset({
    "state", "saint", "tech", "aandm", "poly",
    "northern", "southern", "eastern", "western", "central",
})

# "A&M" tokenizes to two single characters, which the length filter drops —
# leaving "Texas A&M" indistinguishable from "Texas". Fold it to one token
# first so it survives as a discriminator.
_AANDM_RE = re.compile(r"\ba\s*&\s*m\b")

# Alignment scores. Exact-token-set beats subset beats surname-only; the gap
# between MIN_PAIR_SCORE and SUBSET is what lets an exact match win a tie
# (e.g. a hypothetical "New York" that subset-matches two teams).
_EXACT = 1.0
_SUBSET = 0.85
_MIN_PAIR_SCORE = 0.5  # product of both competitors' scores must clear this
_MIN_SIDE_SCORE = 0.6  # and each competitor individually
_TIE_MARGIN = 1e-9


def _initials(name: str) -> frozenset[str]:
    """Single-character tokens — Kalshi's abbreviated club names.

    "New York Y" (Yankees), "Los Angeles D" (Dodgers). These are ABBREVIATIONS
    of a word on the other venue, not words in their own right, which is why
    `_name_tokens` cannot keep them: as ordinary tokens "y" never equals
    "yankees", and the correct pair stops matching. Kept separately and applied
    as a prefix constraint instead — see `competitor_score`.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    folded = _AANDM_RE.sub("aandm", folded.lower())
    return frozenset(t for t in _TOKEN_RE.findall(folded) if len(t) == 1)


def _name_tokens(name: str) -> frozenset[str]:
    """Accent-folded, lowercased word tokens; single chars handled separately
    by `_initials` (they abbreviate a word rather than being one).

    "St." is ambiguous by position, and the two readings are different schools:
    trailing it abbreviates State (Kansas St.), leading it abbreviates Saint
    (St. John's). Both are folded to their full form so the discriminator check
    sees them regardless of which venue's spelling arrived.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    folded = _AANDM_RE.sub("aandm", folded.lower())
    raw = _TOKEN_RE.findall(folded)
    out = []
    for i, t in enumerate(raw):
        if t == "st":
            t = "saint" if i == 0 else "state"
        out.append(t)
    return frozenset(t for t in out if len(t) > 1)


def competitor_score(a: str, b: str) -> float:
    """How confidently two competitor strings name the same team/player.

    Subset handles the systematic cross-venue gap: Kalshi uses city names and
    surnames ("Houston", "Pegula"), Poly uses full names ("Houston Astros",
    "Jessica Pegula"). Partial overlap scores by containment of the smaller
    set, floored below the acceptance threshold unless it's near-total.

    Subset is refused when only one side carries a discriminator token, which
    is what separates "Houston" / "Houston Astros" (same club) from "Kansas" /
    "Kansas St." (different schools).
    """
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    # An initial must be borne out by the other side. Without this, "New York Y"
    # and "New York M" are the SAME token set, so the Yankees and the Mets are
    # indistinguishable, and the subset rule then happily matches either.
    if not _initials_agree(a, b, ta, tb):
        return 0.0
    if ta == tb:
        return _EXACT
    # A discriminator on one side only means these are different programs, not
    # one program named at two levels of detail. Refuse before the subset rule.
    if (ta ^ tb) & _DISCRIMINATORS:
        return 0.0
    if ta <= tb or tb <= ta:
        return _SUBSET
    contained = len(ta & tb) / min(len(ta), len(tb))
    return _SUBSET * contained if contained >= 0.99 else 0.0


def _initials_agree(a: str, b: str, ta: frozenset, tb: frozenset) -> bool:
    """Does every single-letter abbreviation match a word on the other side?

    "New York Y" against "New York Yankees": the unmatched word on the other
    side is "yankees", which starts with y — consistent. Against "New York
    Mets" the unmatched word is "mets", which does not — so these are different
    clubs and the subset rule must not be allowed to pair them on "New York".

    Only the words the other side does NOT share are candidates: shared words
    are already accounted for, and an initial abbreviates the part that differs.
    A side with no leftover word cannot contradict, so it passes.
    """
    for name, own, other in ((a, ta, tb), (b, tb, ta)):
        leftovers = other - own
        for ch in _initials(name):
            if leftovers and not any(w.startswith(ch) for w in leftovers):
                return False
    return True


@dataclass(frozen=True)
class _Game:
    """One venue's view of one game: the competitor pair plus, per competitor
    name, the market whose YES pays on that competitor (and its abbrev)."""

    league: str
    start_time: object  # datetime | None
    competitors: tuple[str, str]
    markets_by_yes: dict[str, Market]  # yes_competitor -> Market
    abbrevs_by_yes: dict[str, str]     # yes_competitor -> venue short code


def _kalshi_games(markets: list[Market]) -> list[_Game]:
    """Group Kalshi per-competitor markets into games by event ticker."""
    groups: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        if m.event is not None:
            # "kalshi:KXMLBGAME-26JUL081845HOUWSH-HOU" -> event prefix
            groups[m.id.rsplit("-", 1)[0]].append(m)
    games = []
    for members in groups.values():
        ev = members[0].event
        # Key each member market by the event-title competitor it pays on:
        # yes_sub_title is the full name ("Jessica Pegula") while the title
        # names are shorter ("Pegula"), and the alignment mapping speaks in
        # title names. A member that matches neither keeps its own name —
        # it can then never be emitted, which is the safe failure.
        markets_by_comp: dict[str, Market] = {}
        abbrevs_by_comp: dict[str, str] = {}
        for m in members:
            yes = m.event.yes_competitor
            comp = max(ev.competitors, key=lambda c: competitor_score(c, yes))
            if competitor_score(comp, yes) < _MIN_SIDE_SCORE:
                comp = yes
            markets_by_comp[comp] = m
            abbrevs_by_comp[comp] = m.event.yes_abbrev
        games.append(_Game(
            league=ev.league,
            start_time=ev.start_time,
            competitors=ev.competitors,
            markets_by_yes=markets_by_comp,
            abbrevs_by_yes=abbrevs_by_comp,
        ))
    return games


def _poly_games(markets: list[Market]) -> list[_Game]:
    """Each Poly moneyline market IS a game (YES = the long side)."""
    return [
        _Game(
            league=m.event.league,
            start_time=m.event.start_time,
            competitors=m.event.competitors,
            markets_by_yes={m.event.yes_competitor: m},
            abbrevs_by_yes={m.event.yes_competitor: m.event.yes_abbrev},
        )
        for m in markets
        if m.event is not None
    ]


def _hours_apart(a, b) -> float | None:
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 3600.0


def _align(kg: _Game, pg: _Game) -> tuple[float, dict[str, str]] | None:
    """Best consistent competitor assignment between two games.

    Returns (score, {poly_competitor: kalshi_competitor}) or None. Score is
    the product of the two competitor scores; abbreviation equality on a pair
    lifts that pair to exact. If the straight and swapped assignments tie
    (same-city derby with token-identical names), the game is ambiguous —
    matching wrong inverts the hedge, so return None.
    """
    (k1, k2), (p1, p2) = kg.competitors, pg.competitors

    def pair(kc: str, pc: str) -> float:
        s = competitor_score(kc, pc)
        ka = kg.abbrevs_by_yes.get(kc, "")
        pa = pg.abbrevs_by_yes.get(pc, "")
        if s > 0 and ka and pa and ka == pa:
            return _EXACT
        return s

    straight = (pair(k1, p1), pair(k2, p2))
    swapped = (pair(k1, p2), pair(k2, p1))
    s_score = straight[0] * straight[1]
    w_score = swapped[0] * swapped[1]
    best, sides, mapping = (
        (s_score, straight, {p1: k1, p2: k2})
        if s_score >= w_score
        else (w_score, swapped, {p2: k1, p1: k2})
    )
    if best < _MIN_PAIR_SCORE or min(sides) < _MIN_SIDE_SCORE:
        return None
    if abs(s_score - w_score) <= _TIE_MARGIN:
        return None  # ambiguous which side is which — refuse to guess
    return best, mapping


class StructuredMatcher:
    """Matches head-to-head game markets on structured identity, not text."""

    def __init__(self, start_tolerance_hours: float = 30.0):
        # 30h absorbs date-only Kalshi tickers (parsed to midnight Eastern)
        # against Poly's precise UTC gameStartTime, and timezone skew — while
        # still separating a team's games on consecutive days (~24h apart but
        # disambiguated by the closest-start rule below).
        self.start_tolerance_hours = start_tolerance_hours

    def match(
        self, kalshi_markets: list[Market], poly_markets: list[Market]
    ) -> list[MatchCandidate]:
        by_league: dict[str, list[_Game]] = defaultdict(list)
        for g in _kalshi_games(kalshi_markets):
            by_league[g.league].append(g)

        candidates: list[MatchCandidate] = []
        for pg in _poly_games(poly_markets):
            # Score every same-league Kalshi game within the start window.
            scored: list[tuple[float, float, _Game, dict[str, str]]] = []
            for kg in by_league.get(pg.league, ()):
                hours = _hours_apart(kg.start_time, pg.start_time)
                if hours is None or hours > self.start_tolerance_hours:
                    continue
                aligned = _align(kg, pg)
                if aligned is not None:
                    scored.append((aligned[0], hours, kg, aligned[1]))
            if not scored:
                continue
            # Same teams can meet twice in the window (MLB doubleheaders,
            # series): highest score wins, closest start breaks score ties.
            scored.sort(key=lambda s: (-s[0], s[1]))
            score, hours, kg, mapping = scored[0]
            if (
                len(scored) > 1
                and abs(scored[1][0] - score) <= _TIE_MARGIN
                and abs(scored[1][1] - hours) <= 1.0
            ):
                continue  # two indistinguishable games — refuse to guess

            # Emit the Kalshi market whose YES competitor is Poly's YES
            # (long) competitor, so YES means the same outcome on both legs.
            poly_yes = next(iter(pg.markets_by_yes))
            kalshi_yes = mapping[poly_yes]
            km = kg.markets_by_yes.get(kalshi_yes)
            pm = pg.markets_by_yes[poly_yes]
            if km is None:
                continue  # Kalshi didn't list a market for that side
            delta = _delta_days(km, pm)
            candidates.append(MatchCandidate(
                kalshi_id=km.id,
                polymarket_id=pm.id,
                kalshi_question=km.question,
                polymarket_question=pm.question,
                similarity_score=round(score, 4),
                resolution_date_delta_days=delta if delta is not None else -1,
                match_method="structured",
            ))
        candidates.sort(key=lambda c: c.similarity_score, reverse=True)
        return candidates
