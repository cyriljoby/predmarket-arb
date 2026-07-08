"""Opportunity logger — appends one JSON line per detected window to disk.

Consumes a `PairEvaluation` (the detector's best-direction funnel record) plus
the match metadata (for the pair ids, question, and `resolution_match`) and
writes the flat record the backtest reads. Every line carries all three spread
fields — `raw_spread_top_of_book`, `raw_spread_depth_adjusted`,
`fee_adjusted_spread` — so the funnel can attribute attrition to slippage vs
fees. Slippage is embedded in the fill prices; there is deliberately no
`slippage_adjusted_profit` field.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from pmarb.config import LOG_PATH
from pmarb.detection.spread import PairEvaluation


def opportunity_record(
    ev: PairEvaluation, match: dict, *, now: datetime | None = None
) -> dict[str, Any]:
    """Build the flat log record for one evaluation + its match metadata."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    strat = f"YES_{ev.yes_platform}_NO_{ev.no_platform}".upper()
    return {
        "timestamp": ts,
        "kalshi_market_id": match.get("kalshi_id"),
        "polymarket_market_id": match.get("polymarket_id"),
        "question": match.get("kalshi_question"),
        "match_method": match.get("match_method"),
        "strategy": strat,
        "yes_platform": ev.yes_platform,
        "no_platform": ev.no_platform,
        "estimated_fillable_size": ev.estimated_fillable_size,
        "yes_ask_top": round(ev.yes_ask_top, 4),
        "no_ask_top": round(ev.no_ask_top, 4),
        "yes_fill_price": round(ev.yes_fill_price, 4),
        "no_fill_price": round(ev.no_fill_price, 4),
        "raw_spread_top_of_book": round(ev.raw_spread_top_of_book, 4),
        "raw_spread_depth_adjusted": round(ev.raw_spread_depth_adjusted, 4),
        "yes_fee_per_share": round(ev.yes_fee_per_share, 4),
        "no_fee_per_share": round(ev.no_fee_per_share, 4),
        "fee_adjusted_spread": round(ev.fee_adjusted_spread, 4),
        "resolution_match": match.get("resolution_match"),
    }


class OpportunityLogger:
    """Append-only JSONL sink for detected opportunity windows.

    Usable as a context manager. Flushes each line so a long live run is
    crash-safe and tailable while running.
    """

    def __init__(self, path: str = LOG_PATH):
        self._path = path
        self._fh = open(path, "a")
        self.count = 0

    def log(self, ev: PairEvaluation, match: dict, *, now: datetime | None = None) -> None:
        self._fh.write(json.dumps(opportunity_record(ev, match, now=now)) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "OpportunityLogger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class LatestOpportunityLog:
    """Keyed opportunity sink — ONE line per pair, always the most recent.

    Re-seeing a pair OVERWRITES its entry rather than appending, so the file is
    a live snapshot of currently-open candidates, not a time series. Good for a
    "what's arb-able right now" view; NOT suitable for the backtest's
    window-duration analysis, which needs the full append-only series (see
    `OpportunityLogger`).

    The in-memory dict is authoritative; the file is rewritten from it at most
    every `flush_interval` seconds (and on close) to bound I/O under a firehose
    of updates.
    """

    def __init__(self, path: str = LOG_PATH, flush_interval: float = 0.5):
        self._path = path
        self._latest: dict[tuple, dict] = {}
        self._flush_interval = flush_interval
        self._last_write = 0.0
        self._fh = open(path, "w")

    def log(self, ev: PairEvaluation, match: dict, *, now: datetime | None = None) -> None:
        rec = opportunity_record(ev, match, now=now)
        self._latest[(rec["kalshi_market_id"], rec["polymarket_market_id"])] = rec
        t = time.monotonic()
        if t - self._last_write >= self._flush_interval:
            self._dump()
            self._last_write = t

    def _dump(self) -> None:
        self._fh.seek(0)
        self._fh.truncate()
        for rec in self._latest.values():
            self._fh.write(json.dumps(rec) + "\n")
        self._fh.flush()

    @property
    def count(self) -> int:
        return len(self._latest)  # distinct pairs currently open

    def close(self) -> None:
        self._dump()
        self._fh.close()

    def __enter__(self) -> "LatestOpportunityLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
