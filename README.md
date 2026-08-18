# predmarket-arb

Phase 1 of a cross-venue arbitrage study on regulated prediction markets. It
streams live order books from **Kalshi** and **Polymarket US**, matches markets
across the two venues, walks both books in tandem to find the largest hedge that
still clears fees, and logs every window to disk. Then it replays the log and
reports how much of the apparent edge was real.

**No orders are placed. No capital is at risk.** The deliverable is a
measurement, not a trading system.

---

## Headline

> Over **30 days** of continuous streaming (2026-07-18 → 2026-08-17), **699**
> cross-venue matched pairs were monitored on live books — 96.9M book updates,
> ~2,040 simultaneous books. **38.6%** of those pairs showed at least one
> fee-adjusted-positive window. After removing pairs that fail resolution
> review, **~31%** is a defensible upper bound on pairs that ever showed real
> hedgeable arb.
>
> That number does not survive contact with the clock. **86% of windows were a
> single sample** — gone before the next observation. Of the windows on
> non-diverged pairs, only **14% persisted 30 seconds**, and the median
> persisting one was worth **$3.76 gross** (364 shares × 1.03¢) before a single
> order was routed.
>
> **The arb exists and is measurable. It is not capturable at retail latency,
> and at these sizes it would not pay for the infrastructure to chase it.**

---

## Where the edge goes

Four things stand between "these two prices look different" and "I made money."
Each was measured separately.

### 1. Matching error — the largest and most dangerous leak

A matching error is not a missed opportunity, it is a **loss**: you believe you
are hedged when you hold two independent bets that can both lose.

An unbiased random sample of 60 monitored pairs (drawn *without* looking at
whether the pair ever showed an edge) was reviewed against both venues' actual
settlement rules: **81.7% true matches, 18.3% diverged.**

The divergences were not string-similarity noise. Every one was a pair that
*looked* right:

| Divergence | Why it breaks the hedge |
|---|---|
| **NPB baseball ties** | NPB games end in ties routinely (12-inning limit). Poly settles a tie at $0.50, Kalshi resolves NO. The "hedge" returns ~$0.50 on ~$1.00 of cost. |
| **UFC draws / no-contests** | ~1% of bouts. Poly $0.50, Kalshi NO. Same failure. |
| **NFL preseason ties** | Preseason has no overtime, so ties are live. Kalshi resolves NO; Poly settles "to the winner" with no tie clause at all. |
| **ITF walkovers** | Not negligible at M15/W35 level. Poly settles a no-start at $0.50; Kalshi requires "a ball has been played" and voids at cost. |
| **F1 fastest-lap reserve drivers** | Poly keeps the market valid and re-points the driver's name at the substitute. Kalshi names the driver. After a substitution the two legs track *different people*. |
| **"Next team" deadline drift** | Kalshi: Kawhi Leonard joins Chicago **before Oct 21**. Poly: **before Oct 23**. A signing on Oct 21–22 resolves Kalshi NO and Poly YES. |
| **Conference vs. national championship** (25 pairs) | Kalshi asks whether a school *qualifies for its conference championship game*; Poly asks whether it *advances to the CFP National Championship*. The futures matcher pairs them on the shared tokens `college/football/championship/game`. |

**The implausible tail of "arb" is almost entirely matcher error.** Of the 1,295
detected windows above 10¢ fee-adjusted, **1,282 (99%) sit on pairs that
resolution review labeled diverged.** Hand review, not a price filter, is what
removes them.

Two artifacts survive review and are worth naming as data-quality, not
opportunity: a NASCAR pair showing 83¢ (Kalshi at $0.01 post-race while Poly
still carried a resting $0.85 bid pre-settlement) and an F1 constructors pair
showing 81¢ at size 1. Both are stale books on markets whose event had ended.

### 2. The vig

Matched pairs sit at a **negative** fee-adjusted spread almost all of the time.
A representative live MLB pair sampled every 30s held −2¢ to −3¢ for its entire
observed life, punctuated by one +27¢ print lasting under 34 seconds when one
venue repriced on a game event and the other had not yet followed. That single
print is the whole shape of the opportunity: not a persistent spread, a
**latency dislocation**.

### 3. Slippage

Slippage does not cost you *pairs*, it costs you *size* — which is why the
A→B step of the funnel shows no attrition. When a pair fails the fee gate the
detector prices the hedge at a single contract, where the depth-walked fill
price *is* the top of book, so `raw_spread_depth_adjusted == raw_spread_top_of_book`
by construction. Read A→B as "no attrition by construction," never as
"slippage is free." Its real cost appears at C, in `estimated_fillable_size`:
average top-of-book spread **5.31¢** → average depth-adjusted **4.19¢** on the
same samples.

### 4. Fees

Both venues charge `θ·p·(1−p)` per contract (Kalshi θ=0.07, Poly US taker
θ=0.05), peaking at $0.50. Combined with the 1¢ slippage buffer the detector
requires, the measured **break-even spread is 3.18¢/share** — against an average
fee-adjusted spread on viable windows of 3.10¢. The opportunity and the cost of
taking it are the same size.

---

## The funnel

Over all 699 tracked pairs, unconditional on review label:

| scope | tracked | in log | A: top-of-book spread | B: after slippage | C: after fees |
|---|---|---|---|---|---|
| overall | 699 | 387 | 51.9% | 51.9% | 38.6% |
| structured (games) | 195 | 195 | 87.7% | 87.7% | 40.0% |
| futures (outrights) | 504 | 192 | 38.1%\* | 38.1%\* | 38.1% |
| excluding reviewed-diverged | 653 | 349 | 49.8% | 49.8% | 36.1% |

\* **Futures A/B are floors, not measurements.** The live driver only appends a
futures sample once the pair is already viable, so a futures pair that had a
positive top-of-book edge which never cleared fees leaves no row in the log.
Structured pairs are sampled on every edge change, so their A/B/C are honest.
This is a property of the collector, not of the market, and it is why the
structured row is the one to trust.

### Persistence — the number that actually decides it

Restricted to windows on non-diverged pairs (8,417 windows across 236 pairs):

| survived | windows | pairs |
|---|---|---|
| any (≥1 sample) | 8,417 | 236 |
| ≥ 30s | 1,185 | 109 |
| ≥ 60s | 861 | 89 |
| ≥ 5 min | 126 | 33 |

Median persisting (≥30s) window: **364 shares at 1.03¢ = $3.76 gross**, before
routing, before a second leg's price moves, before the two-leg execution risk
that Phase 1 explicitly does not model.

---

## Methodology notes (what makes these numbers defensible)

**Two review cohorts, never pooled.** An earlier pass reviewed 93 pairs — but
only pairs the detector had already flagged as viable. Selecting on the outcome
and then reporting "100% of reviewed matches had an edge" is circular, and the
first report did exactly that. Those 93 were re-reviewed here on a rules-level
standard and are reported as a **separate cohort** measuring the *detector's
precision* (81.9% true, 21 unreviewable for lack of captured rules text). The
population true-match rate comes only from the seeded unbiased sample. They
agree closely — resolution divergence is not concentrated in the flagged set —
but they are different questions and the report keeps them apart.

**Review standard.** `resolution_match: true` means settlement agrees on every
outcome with non-negligible probability; residual tail asymmetry (on
cancellation Poly pays last fair market price while Kalshi's captured text is
silent, expected void at cost) is recorded in `resolution_notes` rather than
used to fail the pair. Divergence was decided from the two venues' rules text,
never from whether the pair showed an edge.

**Matcher 1:1 correction.** Each matcher picks the best partner from one side's
point of view, so the other side could be claimed twice — the Kalshi green
jersey market and the Kalshi overall-winner market both claiming Poly's overall
Tour de France winner (1.0 vs 0.6667). The loser is a phantom hedge, and phantoms
produced most of the >20¢ "arb" in the first analysis. `dedupe_one_to_one`
reduces each layer to a strict 1:1 assignment, refusing rather than guessing on
exact ties (ambiguous doubleheader legs). This retracted **185 of 1,138 pairs
(16%)** — 168 green-jersey phantoms and 14 doubleheader ambiguities — and the
backtest drops any log sample on a retracted pair from every numerator *and*
denominator (5,578 samples across 29 pairs).

**Honest denominators.** C% is over all *tracked* pairs (the keyed snapshot,
one line per monitored pair), not over the append log, whose futures throttling
would inflate it.

---

## Running it

```bash
python -m pmarb.main [DURATION_SECONDS]          # live collection (Ctrl+C to stop)
python scripts/export_candidates.py              # rebuild matches.json (dedupes as it matches)
python scripts/dedupe_matches.py [IN] [OUT]      # apply 1:1 pass to an existing matches.json
python -m pmarb.backtest.backtest [LOG] [MATCHES] [LATEST]
pytest tests                                     # 129 tests
```

Credentials live in a gitignored `.env` (see `.env.example`); both venues'
request signing is verified live by `scripts/verify_auth.py`.

Architecture, the fee model, the depth-walk contract, and the matcher design are
documented in module docstrings — start with `pmarb/detection/spread.py` for the
tandem depth walk and `pmarb/matching/futures.py` for the outright matcher.

---

## What this is not

- Not a profitable trading system — Phase 1 places no orders.
- Not evidence that Phase 2 would be profitable. The persistence data argues the
  opposite at retail latency.
- The 18.3% resolution-divergence rate is a point estimate from a 60-pair
  sample, not a census; 83.8% of candidate pairs remain unreviewed.
- Execution risk (leg 1 fills, leg 2 moves) is not modeled anywhere in Phase 1
  and would only subtract from these numbers.
