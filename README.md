# predmarket-arb

Cross-venue arbitrage detection for regulated prediction markets. It streams
live order books from **Kalshi** and **Polymarket US**, matches markets across
the two venues, walks both books in tandem to find the largest hedge that still
clears fees, and logs every window to disk for replay.

**Phase 1: no orders are placed, no capital is at risk.** The deliverable is a
measurement, not a trading system.

---

## Pipeline

```
┌─ DISCOVERY ─ offline, re-run when the market catalog moves ──────────────────┐

  Kalshi REST ──┐
                ├──►  scripts/export_candidates.py
  Poly US REST ─┘        │
                         ├─ StructuredMatcher   games: league + start time + YES side
                         ├─ FuturesMatcher      outrights: competition × entity
                         └─ RuleBasedMatcher    lexical, unstructured markets only
                                │
                                ▼  dedupe_one_to_one — per layer, claims carried forward
                                │
                          matches.json  ◄──  scripts/dedupe_matches.py
                          pairs + review lineage    (same 1:1 pass, applied offline
                                                     to an existing match set)

┌─ COLLECTION ─ long-running ──────────────────────────────────────────────────┐

  Kalshi WS ────┐
                ├──► pmarb/main.py ── in-memory book cache, ~2,000 books
  Poly US WS ───┘         │
                          ▼  on every book update
                    evaluate_pair()   staleness gate, then tandem depth walk
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
    opportunities_latest.jsonl   opportunities.jsonl
    one line per pair, always    the time series, throttled
    (the honest denominator)     (viable always; games on edge-change)

┌─ ANALYSIS ───────────────────────────────────────────────────────────────────┐

    both logs + matches.json ──► pmarb/backtest/backtest.py ──► backtest_report.json
```

There is exactly one path by which order books enter the system — the WebSocket
feeds. REST is used only to discover which markets exist.

---

## Stages

### Discovery — three matchers, in priority order

A matching error is the most dangerous failure in the system: you believe you
are hedged when you hold two independent bets that can both lose. So markets are
paired on **structured identity** wherever both venues encode one, and on text
only where neither does.

| matcher | handles | keys on |
|---|---|---|
| `matching/structured.py` | head-to-head games | league, game start time, competitor pair, and which competitor YES pays on |
| `matching/futures.py` | entity outrights | `(competition, entity)` — with sub-event selectors ("Stage 9", "Round 1") required to match exactly |
| `matching/matcher.py` | everything unstructured | Jaccard + character ratio over the question text |

Lexical matching is deliberately barred from markets that carry a structured
identity: those belong to multi-outcome sets, where fuzzy text matching reliably
pairs the wrong outcome.

Each layer is then reduced to a strict 1:1 assignment by `dedupe_one_to_one`,
with claims carried forward so a looser layer can never re-claim a market a more
precise one already took. Contention resolves on score, then on resolution-date
proximity; only when both tie is the pair refused as ambiguous rather than
guessed at.

A match means "same event, same side" — **not** "same settlement rules." That
judgement is human, recorded per pair as review lineage in `matches.json`.

### Collection — detection on every book update

`pmarb/main.py` holds one WebSocket per venue, keeps every subscribed book in
memory, and on each update re-evaluates that market's matched partner.

`evaluate_pair()` applies, in order:

1. **Staleness gate** — if either leg's snapshot is older than
   `MAX_LEG_STALENESS_SECONDS`, discard. A fresh book compared against a stale
   one is a phantom, not an arb.
2. **Tandem depth walk** (`max_fillable_size`) — walks both ask ladders in
   lockstep to find the largest hedge that still clears the viability gate.
   Slippage is applied first, as depth-walked fill prices; fees are computed on
   those fill prices, with the Kalshi fee accumulated per level rather than
   taken on the average (its `0.07·p·(1−p)` is concave, so averaging overstates).

Two sinks, because they answer different questions:

- **`opportunities_latest.jsonl`** — every tracked pair, always, one line each.
  This is what makes "of all pairs monitored, how many ever went viable" an
  honest fraction.
- **`opportunities.jsonl`** — the append-only time series, throttled to bound
  size. Every viable sample is written; non-viable samples only for live games,
  and only when the edge changed.

### Analysis — replay

`pmarb/backtest/backtest.py` reads both logs plus the current `matches.json`,
drops samples belonging to pairs the matcher has since retracted (from every
numerator *and* denominator), and reports the A → B → C funnel with its caveats
attached as data rather than as prose someone has to remember.

---

## Results so far

A 30-day continuous run (2026-07-18 → 2026-08-17): 96.9M book updates, ~2,040
simultaneous books, 706 matched pairs monitored.

| | |
|---|---|
| pairs that ever showed a fee-adjusted-positive window | **38.2%** |
| resolution-match rate, unbiased 60-pair sample | **81.7%** true / 18.3% diverged |
| windows lasting a single sample | **86%** |
| median persisting (≥30s) window | 364 shares × 1.03¢ = **$3.76 gross** |
| break-even spread | 3.19¢/share, against a 3.10¢ average viable spread |

**The arb is real and measurable. It is not capturable at retail latency**, and
at these sizes it would not pay for the infrastructure to chase it.

Two caveats that matter more than the headline. First, `LOG_HEARTBEAT_SECONDS`
throttles the append log to 30 seconds, so a window shorter than that yields one
row and reads as 0s — the "86% single-sample" figure is bounded by the
instrument, not just the market. Second, 99% of windows above 10¢ sit on pairs
that resolution review labeled diverged: **the implausible tail of apparent arb
is matcher error**, and hand review rather than a price filter is what removes it.

---

## Running it

```bash
python scripts/verify_auth.py                    # confirm both venues sign correctly
python scripts/export_candidates.py              # rebuild matches.json
python -m pmarb.main [DURATION_SECONDS]          # collect (Ctrl+C to stop)
python -m pmarb.backtest.backtest [LOG] [MATCHES] [LATEST]
pytest tests                                     # 131 tests
ruff check .
```

Credentials come from a gitignored `.env`; see `.env.example`. Kalshi signs with
RSA-PSS, Polymarket US with Ed25519, and `scripts/verify_auth.py` proves both
against live endpoints.

Design detail lives in module docstrings — `detection/spread.py` for the depth
walk and fee accumulation, `matching/futures.py` and `matching/structured.py`
for how each identity is extracted from the wire.

---

## What this is not

- Not a profitable trading system — Phase 1 places no orders.
- Not evidence that live execution would be profitable. The persistence data
  argues the opposite at retail latency.
- Execution risk (leg 1 fills, leg 2 moves) is modeled nowhere and would only
  subtract from these numbers.
