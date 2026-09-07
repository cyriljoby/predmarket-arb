"""Queue the pairs whose entity names the matcher had to INFER.

The structural verification in the matchers can only prove they are internally
consistent: it scores `competitor_score` against itself, so it reported zero
violations while "Los Angeles D" matched both the Dodgers and the Angels. The
cases that need a human (and later an agent) are exactly the ones where the
code did not see an exact string match and resolved the name some other way.

So every emitted pair is re-scored to record HOW its names were resolved:

    exact        identical token sets            -> skipped, nothing to judge
    subset       one side is shorter             "Houston" / "Houston Astros"
    initial      a single letter carried it      "Los Angeles D" / "... Dodgers"
    mascot-join  Poly's name and school joined   "The Citadel Bulldogs"
    contained    partial overlap, no subset      "Chicago WS" / "Chicago White Sox"

Rows are then DEDUPED to the question actually being asked — (league, kalshi
name, poly name) — because 5,727 pairs sit on only ~700 games and ~450 players,
and one NFL game carries 165 rungs. Each row records how many pairs it settles.

Output: eval/contract_queue.json, with `same_contract: null` to be filled in.
That answers three things at once: ground truth for a contract agent (there is
none today), an alias table to promote into code, and a defect list.

Run: .venv/bin/python scripts/build_contract_queue.py
"""

from __future__ import annotations

import asyncio
import collections
import json

import aiohttp

from pmarb.config import MATCH_LOG_PATH
from pmarb.credentials import PolymarketUSCredentials
from pmarb.feeds.kalshi import KalshiFeed
from pmarb.feeds.polymarket import PolymarketUSFeed
from pmarb.matching.structured import _initials, _name_tokens, competitor_score

OUT = "eval/contract_queue.json"


def basis(kalshi_name: str, poly_name: str, poly_joined: bool) -> str:
    """How were these two names reconciled? '' means they were not."""
    ta, tb = _name_tokens(kalshi_name), _name_tokens(poly_name)
    if not ta or not tb:
        return "empty"
    if ta == tb:
        return "exact"
    if _initials(kalshi_name) or _initials(poly_name):
        return "initial"
    if poly_joined:
        return "mascot-join"
    if ta <= tb or tb <= ta:
        return "subset"
    if competitor_score(kalshi_name, poly_name) > 0:
        return "contained"
    return "unmatched"


def _poly_joined(market) -> bool:
    """Did `_team_name` join a mascot to a school for this market?

    Detectable after the fact: the venue gave two names, neither containing the
    other. That is the college-football case and nothing else.
    """
    for side in (market.raw.get("market", {}).get("marketSides") or []):
        t = side.get("team") or {}
        n, s = (t.get("name") or "").strip(), (t.get("safeName") or "").strip()
        if not n or not s or n.lower() == s.lower():
            continue
        tn, ts = _name_tokens(n), _name_tokens(s)
        if not (tn <= ts or ts <= tn):
            return True
    return False


def _identity(market):
    """The structured identity a pair was matched on, whichever kind it is."""
    return market.event or market.line or market.prop or market.futures


def _entity_questions(km, pm) -> list[tuple[str, str, str]]:
    """The (label, kalshi name, poly name) judgements a pair rests on."""
    ki, pi = _identity(km), _identity(pm)
    if ki is None or pi is None:
        return []
    out: list[tuple[str, str, str]] = []
    kc = getattr(ki, "competitors", None)
    pc = getattr(pi, "competitors", None)
    if kc and pc:
        # Align before comparing: the venues do not list sides in one order.
        straight = (competitor_score(kc[0], pc[0]) * competitor_score(kc[1], pc[1]))
        swapped = (competitor_score(kc[0], pc[1]) * competitor_score(kc[1], pc[0]))
        pairs = (((kc[0], pc[0]), (kc[1], pc[1])) if straight >= swapped
                 else ((kc[0], pc[1]), (kc[1], pc[0])))
        out += [("competitor", a, b) for a, b in pairs]
    if getattr(ki, "player", None) and getattr(pi, "player", None):
        out.append(("player", ki.player, pi.player))
    if getattr(ki, "entity", None) and getattr(pi, "entity", None):
        out.append(("entity", ki.entity, pi.entity))
    return out


async def main() -> None:
    matches = json.load(open(MATCH_LOG_PATH))
    creds = PolymarketUSCredentials.from_env()
    async with aiohttp.ClientSession() as s:
        kalshi, poly = await asyncio.gather(
            KalshiFeed(s).fetch_markets(),
            PolymarketUSFeed(s, creds).fetch_markets(),
        )
    by_id = {m.id: m for m in kalshi + poly}
    joined = {m.id: _poly_joined(m) for m in poly}

    rows: dict[tuple, dict] = {}
    seen = skipped = 0
    for c in matches:
        km, pm = by_id.get(c["kalshi_id"]), by_id.get(c["polymarket_id"])
        if km is None or pm is None:
            skipped += 1
            continue
        seen += 1
        ident = _identity(km)
        league = getattr(ident, "league", "") or "-"
        for label, kname, pname in _entity_questions(km, pm):
            b = basis(kname, pname, joined.get(pm.id, False))
            if b == "exact":
                continue                      # nothing was inferred
            key = (league, label, kname, pname)
            row = rows.setdefault(key, {
                "league": league, "kind": label, "basis": b,
                "kalshi_name": kname, "polymarket_name": pname,
                "pairs_settled": 0, "match_methods": set(),
                "example": {
                    "kalshi_id": c["kalshi_id"],
                    "polymarket_id": c["polymarket_id"],
                    "kalshi_question": c["kalshi_question"],
                    "polymarket_question": c["polymarket_question"],
                },
                "same_contract": None,        # <- fill this in
                "notes": "",
            })
            row["pairs_settled"] += 1
            row["match_methods"].add(c["match_method"])

    out = sorted(rows.values(), key=lambda r: -r["pairs_settled"])
    for r in out:
        r["match_methods"] = sorted(r["match_methods"])
    json.dump(out, open(OUT, "w"), indent=1)

    print(f"pairs read {seen} (skipped {skipped}: market no longer listed)")
    print(f"entity judgements needing review: {len(out)}")
    print(f"  pairs they settle: {sum(r['pairs_settled'] for r in out)}")
    for b, n in collections.Counter(r["basis"] for r in out).most_common():
        print(f"    {b:12} {n}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
