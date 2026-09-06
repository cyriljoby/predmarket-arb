"""Settlement hazards — where a CORRECTLY matched pair still isn't a hedge.

The matchers answer "same event, same side". That is not the same question as
"these two contracts pay out together", and the 30-day review found the gap is
where the real danger lives: of 47 pairs labeled NOT a resolution match, 20 were
matched perfectly. Same game, same competitor, start times agreeing to the
minute — and still two independent bets, because the venues settle the tail
differently.

Those 20 are not per-market accidents. Nineteen of them are properties of the
COMPETITION: NPB games end in ties, NFL preseason has no overtime, ITF's lowest
tiers are full of walkovers, F1 re-points a fastest-lap market at a substitute
driver. A venue's rules text is not needed to know any of that, and could not be
trusted for it anyway — Kalshi's `rules_primary` sometimes arrives as an
unfilled template ("above || Count || by || Date ||").

So this is a lookup, not a model. It is keyed on the KALSHI SERIES TICKER rather
than on the league, because the series encodes the tier and the market type, and
that distinction turned out to carry the whole signal:

    keyed on series      0 false flags across 243 positive pairs
    keyed on "tennis"    8 false flags   (ATP/WTA main tour reviewed TRUE,
                                          ITF M15/W15/W35 reviewed FALSE)
    keyed on "f1"       10 false flags   (race winner TRUE, fastest lap FALSE)

A hazard is NOT a rejection. The pair is real and worth tracking; what the flag
says is that the hedge has a hole in it, so the flag travels with the match and
the reviewer (or a later size limit) decides what to do about it.

CALIBRATION CAVEAT, and it is a real one: this table was derived FROM those 47
negatives, so "0 false flags" is a lower bound on its error, not a measurement
of it. Several cells rest on two or three pairs. Treat a new hazard row as a
hypothesis until pairs outside the labeled set test it.
"""

from __future__ import annotations

from dataclasses import replace

# Hazard -> the Kalshi series prefixes that carry it, with the reason the review
# recorded. Series prefix is the first "-"-delimited segment of the ticker:
# "kalshi:KXITFMATCH-26AUG12-ABC" -> "KXITFMATCH".
#
# Each entry names what breaks the hedge, not merely that something does. A row
# without a mechanism is a superstition, and the next person cannot check it.
_TIE = "tie_possible"
_WALKOVER = "walkover_void_asymmetry"
_SUBSTITUTE = "substitute_reassignment"

HAZARDS: dict[str, tuple[str, ...]] = {
    # NPB plays a 12-inning limit, so regular-season ties are routine (several
    # per season). Kalshi resolves NO on a tie ("if X wins"); Poly settles at
    # $0.50. A tie returns ~$0.50 on $1.00 of hedged cost — both legs lose.
    "KXNPBGAME": (_TIE,),
    # KBO runs the same 12-inning limit. NOT observed in the labeled set — this
    # row is inference from the shared rule, not evidence, and should be the
    # first one checked against real pairs.
    "KXKBOGAME": (_TIE,),
    # NFL PRESEASON has no overtime, so a tie is live at roughly 2-5%. Kalshi
    # resolves NO; Poly settles "to the winner" with no tie clause, which is
    # undefined rather than NO. Regular-season ties are far rarer but the same
    # asymmetry applies, and the series ticker does not distinguish the two.
    "KXNFLGAME": (_TIE,),
    # MMA draws and majority draws are a live outcome on every card.
    "KXUFCFIGHT": (_TIE,),
    # ITF's bottom tiers (M15/W15/W35) have frequent walkovers and no-starts.
    # Poly settles a no-start at $0.50; Kalshi requires that "a ball has been
    # played" and voids at cost. The main tours are deliberately absent: ATP,
    # WTA and Challenger pairs were all reviewed TRUE, the reviewer judging
    # retirement risk negligible there. That boundary is a probability
    # judgement, not a rules difference, and is the weakest row in this table.
    "KXITFMATCH": (_WALKOVER,),
    "KXITFWMATCH": (_WALKOVER,),
    # Poly keeps a fastest-lap market valid and re-points the driver's name at
    # a substitute; Kalshi names the driver with no such clause. On a
    # substitution the two legs track DIFFERENT drivers. Race-winner and
    # constructor markets on the same series carry no such clause and are
    # correctly absent.
    "KXF1FASTLAP": (_SUBSTITUTE,),
}


def kalshi_series(market_id: str) -> str:
    """The series prefix of a Kalshi market id.

    "kalshi:KXMLBGAME-26JUL081845HOUWSH-HOU" -> "KXMLBGAME". Anything that is
    not a Kalshi id returns "", so a caller can pass either leg blindly.
    """
    if not market_id.startswith("kalshi:"):
        return ""
    return market_id.split(":", 1)[1].split("-", 1)[0]


def settlement_hazards(kalshi_id: str) -> tuple[str, ...]:
    """Known settlement divergences for a pair, from its Kalshi leg.

    Empty means "none in the table" — which is NOT the same as "none exist".
    The table covers what 317 reviewed pairs surfaced; an unreviewed
    competition has simply never been looked at. Soccer moneylines are the
    known blind spot: draws are the most common tie in sport and the labeled
    set contains no soccer game pairs at all, only outrights.
    """
    return HAZARDS.get(kalshi_series(kalshi_id), ())


def annotate(candidates: list) -> list:
    """Stamp each candidate with the hazards its Kalshi leg carries.

    Applied once, after deduping, rather than inside each matcher: a hazard is
    a property of the competition, not of how the pair was found, and three
    matchers stamping it independently is three places for it to drift.
    """
    return [replace(c, settlement_hazards=settlement_hazards(c.kalshi_id))
            for c in candidates]
