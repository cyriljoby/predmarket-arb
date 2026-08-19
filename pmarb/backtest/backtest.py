"""Backtest — replay opportunities.jsonl and report the honest funnel.

Answers "does this arb actually exist at retail scale": of the apparent
(top-of-book) edges, how much survives slippage (depth-walked fills), and how
much survives fees too. Reads only fields that exist in the log; the single
viability gate is `fee_adjusted_spread` (> 0 AND estimated_fillable_size > 0).

Run: .venv/bin/python -m pmarb.backtest.backtest [LOG] [MATCHES] [LATEST]
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
from pmarb.models import Sample

REPORT_PATH = "backtest_report.json"
# A viable sample this much apart from the previous one (seconds) starts a new
# window rather than extending the old one (heartbeat is 30s; allow 3 missed).
WINDOW_GAP_SECONDS = 120.0


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _avg(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _windows(rows: list[Sample], labels: dict) -> list[dict]:
    """Group each pair's viable samples into contiguous windows; return one
    record per window with its duration (span of its samples, seconds)."""
    by_pair: dict[tuple, list[Sample]] = defaultdict(list)
    for r in rows:
        if r.viable:
            by_pair[r.pair].append(r)
    windows = []
    for pair, samples in by_pair.items():
        samples.sort(key=lambda s: s.observed_at)
        current: list[Sample] = []
        for r in samples:
            if current and (r.observed_at - current[-1].observed_at
                            ).total_seconds() > WINDOW_GAP_SECONDS:
                windows.append(_close_window(pair, current, labels))
                current = []
            current.append(r)
        if current:
            windows.append(_close_window(pair, current, labels))
    return windows


def _close_window(pair: tuple, samples: list[Sample], labels: dict) -> dict:
    span = (samples[-1].observed_at - samples[0].observed_at).total_seconds()
    return {
        "pair": pair,
        # `samples` is what distinguishes an honestly-brief window from one that
        # was only ever seen once. A duration of 0s with a single sample means
        # "unknown, bounded below by the sampling rate", not "instantaneous".
        "samples": len(samples),
        "duration_seconds": round(span, 1),
        "best_fee_adjusted_spread": max(s.spread_fee_adj for s in samples),
        "best_size": max(s.fillable_size for s in samples),
        "question": samples[0].question,
        "resolution_match": labels.get(pair),
    }


def load_from_files(log_path: str = LOG_PATH,
                    match_path: str = MATCH_LOG_PATH,
                    latest_path: str = LATEST_LOG_PATH) -> tuple[list, list, set]:
    """Read a backtest's inputs off disk. One of two sources; see db/sources.py.

    Keeping this path working is what makes the Phase 1 result reproducible:
    the published numbers came from JSONL, and they must stay recomputable from
    the frozen bundle after the collector moves to Postgres.
    """
    rows = [_sample_from_log(json.loads(line))
            for line in open(log_path) if line.strip()]
    # The append log only samples interesting pairs; the honest "of all pairs
    # monitored, how many ever went viable" denominator is the tracked set.
    try:
        tracked = {_log_pair(json.loads(line))
                   for line in open(latest_path) if line.strip()}
    except FileNotFoundError:
        tracked = set()
    try:
        matches = json.load(open(match_path))
    except FileNotFoundError:
        matches = []
    return rows, matches, tracked


def _log_pair(d: dict) -> tuple[str, str]:
    """Pair key from a legacy log line, which used two spellings over its life."""
    return (d["kalshi_market_id"],
            d.get("polymarket_id") or d["polymarket_market_id"])


def _sample_from_log(d: dict) -> Sample:
    """Adapt a Phase 1 JSONL line onto the schema's vocabulary.

    The legacy format is venue-specific (kalshi_/polymarket_) and predates
    sample_reason and the captured resolution dates, so those stay None. This
    adapter exists so the frozen Phase 1 bundle keeps replaying; the DATABASE is
    canonical, not this shape.
    """
    a, b = _log_pair(d)
    return Sample(
        observed_at=datetime.fromisoformat(d["timestamp"]),
        market_a_id=a, market_b_id=b,
        spread_top=d["raw_spread_top_of_book"],
        spread_depth=d["raw_spread_depth_adjusted"],
        spread_fee_adj=d["fee_adjusted_spread"],
        fillable_size=d["estimated_fillable_size"],
        question=d.get("question", ""),
        match_method=d.get("match_method", ""),
        yes_venue=d.get("yes_platform", ""),
        ask_top_yes=d.get("yes_ask_top"), ask_top_no=d.get("no_ask_top"),
        fill_yes=d.get("yes_fill_price"), fill_no=d.get("no_fill_price"),
        fee_yes=d.get("yes_fee_per_share"), fee_no=d.get("no_fee_per_share"),
    )


def run_backtest(rows: list[Sample], matches: list[dict],
                 tracked: set | None = None, *, source: str = "") -> dict:
    """Compute the funnel from already-loaded inputs.

    Takes data, not paths, so a file reader and a database cursor are two
    callers of ONE implementation. The alternative — recomputing the funnel in
    SQL for the API — means two definitions of subtle rules (cohort separation,
    A==B by construction, the futures floor) that drift silently. The failure
    mode there is not a crash, it is a plausible wrong percentage, and this
    project has already shipped one of those.
    """
    tracked = set(tracked or ())
    if not rows:
        raise SystemExit(f"no samples to backtest ({source or 'unknown source'})")

    # The collector streams whatever match set existed when it launched; the
    # match file is the current, corrected one. A pair the matcher has since
    # retracted (a phantom that shared a market with a better hedge, an
    # ambiguous doubleheader leg) was never a hedge, so its samples must not
    # count in any numerator OR denominator.
    if matches:
        valid = {(m["kalshi_id"], m["polymarket_id"]) for m in matches}
        kept_rows = [r for r in rows if r.pair in valid]
        retracted_rows = len(rows) - len(kept_rows)
        retracted_pairs = len({r.pair for r in rows} - valid)
        rows = kept_rows
        tracked &= valid
        if not rows:
            raise SystemExit(
                f"no sample survives the current match set ({source or 'source'})")
    else:
        retracted_rows = retracted_pairs = 0
    n_cand = len(matches)
    # Labels are read from the CURRENT match set, never from the sample. A row
    # records what was believed when it was written; a verdict can be revised
    # afterwards, and 93 pairs were re-reviewed under a stricter standard after
    # the 30-day run. Samples stay immutable and the label is looked up by pair.
    labels = {(m["kalshi_id"], m["polymarket_id"]): m.get("resolution_match")
              for m in matches}

    # Two review cohorts, reported SEPARATELY because they answer different
    # questions and only one of them is an unbiased estimator:
    #   - unbiased-tracked: a seeded random sample of monitored pairs, drawn
    #     WITHOUT looking at whether the pair ever showed an edge. This is the
    #     population true-match rate.
    #   - detector-flagged: every pair the detector called viable. Selected ON
    #     the outcome, so it measures the SIGNAL'S PRECISION, not the population.
    # Pooling them would launder the selection bias, so they never mix.
    cohorts = {}
    for name in ("unbiased-tracked", "detector-flagged"):
        c = [m for m in matches
             if str(m.get("review_sample", "")).startswith(name)]
        t = sum(1 for m in c if m.get("resolution_match") is True)
        f = sum(1 for m in c if m.get("resolution_match") is False)
        cohorts[name] = {
            "reviewed": t + f,
            "true": t,
            "diverged": f,
            "true_pct": _pct(t, t + f),
            "diverged_pct": _pct(f, t + f),
            # Pairs pulled into the cohort but with no resolution text captured.
            "unreviewable": len(c) - t - f,
        }

    # --- A -> B -> C funnel over EVERY tracked pair ------------------------ #
    # Unconditional on the review label: the label rate is applied once, at the
    # headline, from the unbiased cohort. Conditioning the funnel on
    # `resolution_match is True` would restrict it to reviewed pairs, and the
    # bulk of those were reviewed BECAUSE they went viable — that is what made
    # the old report print A=B=C=100%.
    by_pair = defaultdict(list)
    for r in rows:
        by_pair[r.pair].append(r)
    tracked |= set(by_pair)
    method = {(m["kalshi_id"], m["polymarket_id"]): m.get("match_method")
              for m in matches}

    def _funnel(pairs: set) -> dict:
        a = sum(1 for p in pairs
                if any(r.spread_top > 0 for r in by_pair.get(p, ())))
        b = sum(1 for p in pairs
                if any(r.spread_depth > 0 for r in by_pair.get(p, ())))
        c = sum(1 for p in pairs
                if any(r.viable for r in by_pair.get(p, ())))
        return {
            "tracked": len(pairs),
            "sampled_in_log": sum(1 for p in pairs if p in by_pair),
            "A_raw_top_of_book_spread_pct": _pct(a, len(pairs)),
            "B_spread_after_slippage_pct": _pct(b, len(pairs)),
            "C_viable_after_fees_pct": _pct(c, len(pairs)),
            "C_pairs": c,
        }

    structured_pairs = {p for p in tracked if method.get(p) == "structured"}
    futures_pairs = {p for p in tracked if method.get(p) == "futures"}
    # Dropping pairs REVIEWED AS DIVERGED is not the selection bias the old
    # report had: divergence was decided from the two venues' rules text, never
    # from whether the pair showed an edge. Unreviewed pairs stay in, so this is
    # still an upper bound — just one with the known-bad hedges taken out.
    clean_pairs = {p for p in tracked if labels.get(p) is not False}
    funnel = {
        "scope": "all tracked pairs, unconditional on review label",
        "overall": _funnel(tracked),
        "structured": _funnel(structured_pairs),
        "futures": _funnel(futures_pairs),
        "excluding_reviewed_diverged": _funnel(clean_pairs),
        "caveats": [
            # Both are properties of the collector, not of the market.
            "futures A/B are floors, not measurements: the live driver only "
            "appends a futures sample when the pair is already viable, so a "
            "futures pair that had a positive top-of-book edge which never "
            "cleared fees leaves no row. Structured pairs are sampled on every "
            "edge change, so their A/B/C are honest.",
            "A and B coincide whenever a pair never clears the fee gate: "
            "evaluate_pair then prices the hedge at a single contract, where "
            "the depth-walked fill IS the top of book. Slippage therefore does "
            "not show up as pairs lost between A and B — it shows up as size "
            "lost at C. Read the A->B step as 'no attrition by construction', "
            "not as 'slippage is free'.",
        ],
    }

    # --- economics on viable samples --------------------------------------- #
    viable_rows = [r for r in rows if r.viable]
    sizes = sorted(r.fillable_size for r in viable_rows)
    fees = [r.total_fee for r in rows]

    # --- time structure ----------------------------------------------------- #
    times = sorted(r.observed_at for r in rows)
    span_h = (times[-1] - times[0]).total_seconds() / 3600 if len(times) > 1 else 0.0
    windows = _windows(rows, labels)
    durations = sorted(w["duration_seconds"] for w in windows)
    single = sum(1 for w in windows if w["samples"] == 1)

    # --- headline ----------------------------------------------------------- #
    # Apparent arb -> real arb. The C% is measured; the resolution haircut is
    # ESTIMATED by applying the unbiased cohort's true rate, so it is a point
    # estimate off a 60-pair sample, not a census.
    ever_viable_pct = funnel["overall"]["C_viable_after_fees_pct"]
    true_rate = cohorts["unbiased-tracked"]["true_pct"] / 100.0
    headline = {
        "tracked_pairs": funnel["overall"]["tracked"],
        "ever_viable_pct": ever_viable_pct,
        "resolution_true_rate_pct": cohorts["unbiased-tracked"]["true_pct"],
        "real_arb_pct": round(ever_viable_pct * true_rate, 1),
        "median_window_duration_seconds": median(durations) if durations else None,
        "single_sample_window_pct": _pct(single, len(windows)),
        "statement": (
            f"{ever_viable_pct}% of tracked matched pairs showed at least one "
            f"fee-adjusted-positive window in {round(span_h / 24, 1)} days; "
            f"applying the {cohorts['unbiased-tracked']['true_pct']}% "
            f"resolution-match rate from the unbiased sample leaves "
            f"{round(ever_viable_pct * true_rate, 1)}% as real hedgeable arb — "
            f"and {_pct(single, len(windows))}% of those windows were a single "
            f"sample, i.e. gone before a second look."
        ),
    }

    report = {
        "source": source,
        "samples": len(rows),
        "retracted_by_match_set": {
            "samples": retracted_rows,
            "pairs": retracted_pairs,
        },
        "collection_span_hours": round(span_h, 2),
        "headline": headline,
        "resolution_review": {
            "candidate_matches": n_cand,
            "unreviewed_pct": _pct(
                sum(1 for v in labels.values() if v is None), n_cand),
            "cohorts": cohorts,
        },
        "funnel": funnel,
        "spreads": {
            "avg_raw_top_of_book_when_positive": _avg(
                [r.spread_top for r in rows if r.spread_top > 0]),
            "avg_depth_adjusted_when_positive": _avg(
                [r.spread_depth for r in rows if r.spread_depth > 0]),
            "avg_fee_adjusted_on_viable": _avg(
                [r.spread_fee_adj for r in viable_rows]),
            "break_even_spread": round((_avg(fees) or 0.0) + SLIPPAGE_BUFFER, 4),
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
            "single_sample_windows": single,
            "detail": sorted(windows,
                             key=lambda w: w["best_fee_adjusted_spread"],
                             reverse=True),
        },
    }
    return report


def _print_summary(rep: dict) -> None:
    hl, rr, fn, sp, wd = (rep["headline"], rep["resolution_review"],
                          rep["funnel"], rep["spreads"], rep["windows"])
    print(f"samples: {rep['samples']} over {rep['collection_span_hours']}h "
          f"({fn['overall']['tracked']} tracked pairs)")
    rt = rep["retracted_by_match_set"]
    if rt["samples"]:
        print(f"  (dropped {rt['samples']} samples on {rt['pairs']} pairs the "
              f"matcher has since retracted)")

    print(f"\nresolution review ({rr['candidate_matches']} candidates, "
          f"{rr['unreviewed_pct']}% still unreviewed):")
    for name, c in rr["cohorts"].items():
        print(f"  {name:17} n={c['reviewed']:<4} "
              f"{c['true_pct']}% true / {c['diverged_pct']}% diverged"
              + (f"  ({c['unreviewable']} unreviewable)"
                 if c["unreviewable"] else ""))

    print(f"\nfunnel [{fn['scope']}]:")
    print(f"  {'':28} {'tracked':>7} {'logged':>7} {'A':>7} {'B':>7} {'C':>7}")
    for key in ("overall", "structured", "futures",
                "excluding_reviewed_diverged"):
        f = fn[key]
        note = "*" if key == "futures" else " "
        print(f"  {key:28} {f['tracked']:>7} {f['sampled_in_log']:>7} "
              f"{f['A_raw_top_of_book_spread_pct']:>6}%{note}"
              f"{f['B_spread_after_slippage_pct']:>6}%{note}"
              f"{f['C_viable_after_fees_pct']:>6}%")
    print("  * futures A/B are floors — non-viable futures samples are never "
          "logged (see report caveats)")

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

    print(f"\nHEADLINE: {hl['statement']}")


def main() -> None:
    log_path = sys.argv[1] if len(sys.argv) > 1 else LOG_PATH
    match_path = sys.argv[2] if len(sys.argv) > 2 else MATCH_LOG_PATH
    latest_path = sys.argv[3] if len(sys.argv) > 3 else LATEST_LOG_PATH
    rows, matches, tracked = load_from_files(log_path, match_path, latest_path)
    report = run_backtest(rows, matches, tracked,
                          source=f"{log_path} + {match_path}")
    _print_summary(report)
    with open(REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nfull report -> {REPORT_PATH}")


if __name__ == "__main__":
    main()
