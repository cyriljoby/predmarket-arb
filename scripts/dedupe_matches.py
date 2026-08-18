"""Apply the 1:1 assignment pass to an ALREADY-WRITTEN matches.json.

`export_candidates.py` now dedupes as it matches, but the 30-day collection run
streamed the pre-dedupe match set, so its opportunity log contains phantom pairs
(one market anchoring two "hedges"). Re-running the matcher live would not
reproduce that market universe a month later, so the correction is applied
offline to the same file the run consumed. Any review labels already recorded on
a surviving pair are preserved.

Run: .venv/bin/python scripts/dedupe_matches.py [IN] [OUT]
"""

import json
import sys
from collections import Counter

from pmarb.matching.matcher import MatchCandidate, dedupe_one_to_one

# Same priority order as the live pipeline: most precise layer claims first.
_PRIORITY = ("structured", "futures", "lexical")
_FIELDS = set(MatchCandidate.__dataclass_fields__)


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else "matches.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "matches_deduped.json"

    raw = json.load(open(src))
    cands = [MatchCandidate(**{k: v for k, v in m.items() if k in _FIELDS})
             for m in raw]

    kept: list[MatchCandidate] = []
    claimed_k: set[str] = set()
    claimed_p: set[str] = set()
    for method in _PRIORITY:
        group = [c for c in cands if c.match_method == method]
        kept.extend(dedupe_one_to_one(group, claimed_k, claimed_p))

    with open(dst, "w") as f:
        json.dump([c.__dict__ for c in kept], f, indent=2)

    before, after = Counter(c.match_method for c in cands), Counter(
        c.match_method for c in kept)
    for method in _PRIORITY:
        if before[method]:
            print(f"  {method:11} {before[method]:5} -> {after[method]:5} "
                  f"({before[method] - after[method]} dropped)")
    print(f"  {'TOTAL':11} {len(cands):5} -> {len(kept):5}")
    print(f"wrote {dst}")


if __name__ == "__main__":
    main()
