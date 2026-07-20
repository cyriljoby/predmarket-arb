"""Backtest — replay opportunities.jsonl and report the honest funnel.

Answers "does this arb actually exist at retail scale": of the apparent
(top-of-book) edges, how much survives slippage (depth-walked fills), and how
much survives fees too. Reads only fields that exist in the log; the single
viability gate is `fee_adjusted_spread` (> 0 AND estimated_fillable_size > 0).

Run: .venv/bin/python -m pmarb.backtest.backtest [opportunities.jsonl]
Writes backtest_report.json next to the log.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from statistics import median, quantiles

from pmarb.config import (
    LATEST_LOG_PATH,
    LOG_PATH,
    MATCH_LOG_PATH,
    SLIPPAGE_BUFFER,
)

REPORT_PATH = "backtest_report.json"
# A viable sample this much apart from the previous one (seconds) starts a new
# window rather than extending the old one (heartbeat is 30s; allow 3 missed).
WINDOW_GAP_SECONDS = 120.0


def _pair(row: dict) -> tuple[str, str]:
    return (row["kalshi_market_id"], row["polymarket_id"]) if "polymarket_id" in row \
        else (row["kalshi_market_id"], row["polymarket_market_id"])


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _avg(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _viable(row: dict) -> bool:
    return row["fee_adjusted_spread"] > 0 and row["estimated_fillable_size"] > 0


def _windows(rows: list[dict]) -> list[dict]:
    """Group each pair's viable samples into contiguous windows; return one
    record per window with its duration (span of its samples, seconds)."""
    by_pair: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if _viable(r):
            by_pair[_pair(r)].append(r)
    windows = []
    for pair, samples in by_pair.items():
        samples.sort(key=lambda r: r["timestamp"])
        current: list[dict] = []
        for r in samples:
            t = datetime.fromisoformat(r["timestamp"])
            if current and (t - datetime.fromisoformat(current[-1]["timestamp"])
                            ).total_seconds() > WINDOW_GAP_SECONDS:
                windows.append(_close_window(pair, current))
                current = []
            current.append(r)
        if current:
            windows.append(_close_window(pair, current))
    return windows


def _close_window(pair: tuple, samples: list[dict]) -> dict:
    t0 = datetime.fromisoformat(samples[0]["timestamp"])
    t1 = datetime.fromisoformat(samples[-1]["timestamp"])
    return {
        "pair": pair,
        "samples": len(samples),
        "duration_seconds": round((t1 - t0).total_seconds(), 1),
        "best_fee_adjusted_spread": max(s["fee_adjusted_spread"] for s in samples),
        "best_size": max(s["estimated_fillable_size"] for s in samples),
        "question": samples[0].get("question", ""),
        "resolution_match": samples[0].get("resolution_match"),
    }


def run_backtest(log_path: str = LOG_PATH,
                 match_path: str = MATCH_LOG_PATH,
                 latest_path: str = LATEST_LOG_PATH) -> dict:
    rows = [json.loads(line) for line in open(log_path) if line.strip()]
    # The append log only samples interesting pairs; the honest "of all pairs
    # monitored, how many ever went viable" denominator is the tracked set.
    try:
        tracked = {_pair(json.loads(l)) for l in open(latest_path) if l.strip()}
    except FileNotFoundError:
        tracked = set()
    if not rows:
        raise SystemExit(f"{log_path} is empty — nothing to backtest")

    # --- resolution-match funnel (X/Y/Z) over candidate matches ------------ #
    try:
        matches = json.load(open(match_path))
    except FileNotFoundError:
        matches = []
    n_cand = len(matches)
    n_true = sum(1 for m in matches if m.get("resolution_match") is True)
    n_false = sum(1 for m in matches if m.get("resolution_match") is False)
    n_null = n_cand - n_true - n_false

    # --- A/B/C funnel over logged pairs ------------------------------------ #
    # Spec: computed over TRUE resolution matches only. While review hasn't
    # happened yet (all null), fall back to all logged pairs and say so —
    # numbers are then an upper bound on the real funnel.
    labels = {(m["kalshi_id"], m["polymarket_id"]): m.get("resolution_match")
              for m in matches}
    reviewed = any(v is not None for v in labels.values())
    for r in rows:  # log rows carry the label as of log time; use current labels
        r["resolution_match"] = labels.get(_pair(r), r.get("resolution_match"))
    in_scope = [r for r in rows
                if not reviewed or labels.get(_pair(r)) is True]

    pairs = defaultdict(list)
    for r in in_scope:
        pairs[_pair(r)].append(r)

    a_pairs = {p for p, rs in pairs.items()
               if any(r["raw_spread_top_of_book"] > 0 for r in rs)}
    b_pairs = {p for p, rs in pairs.items()
               if any(r["raw_spread_depth_adjusted"] > 0 for r in rs)}
    c_pairs = {p for p, rs in pairs.items() if any(_viable(r) for r in rs)}
    n_pairs = len(pairs)

    # --- economics on viable samples --------------------------------------- #
    viable_rows = [r for r in in_scope if _viable(r)]
    sizes = sorted(r["estimated_fillable_size"] for r in viable_rows)
    fees = [r["yes_fee_per_share"] + r["no_fee_per_share"] for r in in_scope]

    # --- time structure ----------------------------------------------------- #
    times = sorted(datetime.fromisoformat(r["timestamp"]) for r in rows)
    span_h = (times[-1] - times[0]).total_seconds() / 3600 if len(times) > 1 else 0.0
    windows = _windows(in_scope)
    durations = sorted(w["duration_seconds"] for w in windows)

    report = {
        "log_path": log_path,
        "samples": len(rows),
        "collection_span_hours": round(span_h, 2),
        "resolution_review": {
            "candidate_matches": n_cand,
            "true_pct": _pct(n_true, n_cand),
            "diverged_pct": _pct(n_false, n_cand),
            "unreviewed_pct": _pct(n_null, n_cand),
        },
        "funnel_scope": "resolution_match true only" if reviewed
                        else "ALL logged pairs (nothing reviewed yet — upper bound)",
        "funnel": {
            "pairs_logged": n_pairs,
            "A_raw_top_of_book_spread_pct": _pct(len(a_pairs), n_pairs),
            "B_spread_after_slippage_pct": _pct(len(b_pairs), n_pairs),
            "C_viable_after_fees_pct": _pct(len(c_pairs), n_pairs),
        },
        # Against every pair the collector monitored (bounded snapshot set) —
        # the defensible "what fraction of matched markets ever showed real,
        # resolution-verified arb" number.
        "of_all_tracked_pairs": {
            "tracked": len(tracked | set(pairs)),
            "ever_viable_pct": _pct(len(c_pairs), len(tracked | set(pairs))),
        },
        "spreads": {
            "avg_raw_top_of_book_when_positive": _avg(
                [r["raw_spread_top_of_book"] for r in in_scope
                 if r["raw_spread_top_of_book"] > 0]),
            "avg_depth_adjusted_when_positive": _avg(
                [r["raw_spread_depth_adjusted"] for r in in_scope
                 if r["raw_spread_depth_adjusted"] > 0]),
            "avg_fee_adjusted_on_viable": _avg(
                [r["fee_adjusted_spread"] for r in viable_rows]),
            "break_even_spread": round(
                (_avg(fees) or 0.0) + SLIPPAGE_BUFFER, 4),
        },
        "fillable_size_on_viable": {
            "median": median(sizes) if sizes else None,
            "p25": quantiles(sizes, n=4)[0] if len(sizes) >= 4 else None,
            "p75": quantiles(sizes, n=4)[2] if len(sizes) >= 4 else None,
        },
        "windows": {
            "count": len(windows),
            "per_hour": round(len(windows) / span_h, 2) if span_h else None,
            "median_duration_seconds": median(durations) if durations else None,
            "single_sample_windows": sum(1 for w in windows if w["samples"] == 1),
            "detail": sorted(windows,
                             key=lambda w: w["best_fee_adjusted_spread"],
                             reverse=True),
        },
    }
    return report


def _print_summary(rep: dict) -> None:
    rr, fn, sp, wd = (rep["resolution_review"], rep["funnel"],
                      rep["spreads"], rep["windows"])
    print(f"samples: {rep['samples']} over {rep['collection_span_hours']}h "
          f"({fn['pairs_logged']} distinct pairs)")
    print(f"\nresolution review of {rr['candidate_matches']} candidates: "
          f"{rr['true_pct']}% true / {rr['diverged_pct']}% diverged / "
          f"{rr['unreviewed_pct']}% unreviewed")
    print(f"\nfunnel [{rep['funnel_scope']}]:")
    print(f"  A  raw top-of-book spread : {fn['A_raw_top_of_book_spread_pct']}%")
    print(f"  B  survives slippage      : {fn['B_spread_after_slippage_pct']}%")
    print(f"  C  survives fees too      : {fn['C_viable_after_fees_pct']}%")
    at = rep["of_all_tracked_pairs"]
    print(f"  of ALL {at['tracked']} tracked pairs, ever viable: "
          f"{at['ever_viable_pct']}%")
    print(f"\nspreads: raw={sp['avg_raw_top_of_book_when_positive']} "
          f"post-slippage={sp['avg_depth_adjusted_when_positive']} "
          f"viable-avg={sp['avg_fee_adjusted_on_viable']} "
          f"break-even={sp['break_even_spread']}")
    fs = rep["fillable_size_on_viable"]
    print(f"viable size: median={fs['median']} p25={fs['p25']} p75={fs['p75']}")
    print(f"windows: {wd['count']} ({wd['per_hour']}/h), "
          f"median duration {wd['median_duration_seconds']}s, "
          f"{wd['single_sample_windows']} single-sample")
    print("\ntop windows by fee-adjusted spread:")
    for w in wd["detail"][:10]:
        label = {True: "OK", False: "DIVERGED", None: "unreviewed"}[
            w["resolution_match"]]
        print(f"  ${w['best_fee_adjusted_spread']:.4f} x{w['best_size']:<5} "
              f"{w['duration_seconds']:>6.0f}s [{label}] {w['question'][:52]}")


def main() -> None:
    log_path = sys.argv[1] if len(sys.argv) > 1 else LOG_PATH
    report = run_backtest(log_path)
    _print_summary(report)
    with open(REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nfull report -> {REPORT_PATH}")


if __name__ == "__main__":
    main()
