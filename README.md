# predmarket-arb

## Purpose

Cross-venue arbitrage **detection and measurement** for regulated prediction
markets. It streams live order books from **Kalshi** and **Polymarket US**,
matches markets across the two venues, walks both books in tandem to find the
largest hedge that still clears fees, and records every window for replay.

No orders are placed and no capital is at risk. The deliverable is a
measurement, not a trading system: the question it answers is whether this arb
actually exists at retail scale, once matching error, slippage and fees have all
been charged against it. Results live in `backtest_report.json` and are quoted
with the run that produced them rather than here, because runs differ in match
set, sampling policy and coverage, and their numbers are not interchangeable.

## Architecture

```
pmarb/
  feeds/        kalshi.py, polymarket.py — REST discovery + one WebSocket per venue
  models/       Market (shared schema), SportsEvent / FuturesEvent / LineEvent /
                PropEvent identities, Sample
  matching/     structured, futures, lines, props, matcher (lexical), hazards
  detection/    spread.py — compute_fill_price, max_fillable_size, evaluate_pair
  oplog/        JSONL sinks
  db/           Postgres schema, sync, batched writer
  backtest/     backtest.py (funnel report), survival.py (window survival curve)
  refresh.py    MatchSetRefresher — daily in-process match-set refresh
  main.py       collector: book cache, write-grain policy, detection on each update
scripts/        export_candidates.py, seed_db.py, dedupe_matches.py, verify_auth.py
```

```
REST catalogs ──► matchers ──► matches.json ──┐   (re-read daily by refresh.py)
                                              ▼
Kalshi WS   ──┐                          evaluate_pair ──┬─► opportunities_latest.jsonl
              ├──► normalize to Market ──► (staleness,   │   opportunities.jsonl
Poly US WS  ──┘      + book cache          depth walk)   └─► Postgres observations
                                                                    │
                                                        backtest ◄──┘
```

Matching is offline and produces `matches.json`; the collector consumes it, keeps
every subscribed book in memory, and on each update re-evaluates the matched
partner of the market that ticked. Detection is pure functions over two `Market`
objects, so the economics are testable without a network. Everything observed is
written twice, to files and to Postgres, and the backtest replays either.

## Ingestion and normalization

One authenticated WebSocket feed per venue carries all order-book data. REST is
used only to discover which markets exist — at startup and on each daily refresh.

**Kalshi** sends one `orderbook_snapshot` per ticker and then incremental
`orderbook_delta`s, so book state is **maintained in process**: a snapshot
replaces a ticker's ladders and a delta adjusts one level. A single monotonic
`seq` per subscription is the integrity check — a gap means a message was
dropped, which forces a reconnect and a full resnapshot rather than trusting a
book that silently diverged. Control acks (including live subscription
mutations) consume numbers in that same counter, so the reader advances `seq` on
them too; not doing so turned every mutation into a spurious resnapshot of the
whole subscription.

**Polymarket US** sends a **full snapshot in every frame**, so there is no delta
state to keep and a dropped update is simply superseded by the next one. That is
why the queue merging its shards is deliberately bounded and lossy: an unbounded
queue would trade a silent subscription loss for a silent memory leak, and the
staleness gate already covers the gap.

Poly subscriptions are **sharded across connections** because the venue caps
subscriptions per connection (2,000; the collector shards below it). The excess is
refused in an error frame while the socket stays healthy — **a refused
subscription is otherwise completely silent**, indistinguishable from an idle
market, so the reader logs error frames. Each shard reconnects independently, so
a drop costs that shard's books rather than the whole stream.

Normalization is pure, and reduces both venues to the same `Market`: `yes_depth`
and `no_depth` as **ask-side ladders**, with `no_depth` derived from the YES bids
at `(1 - price)` — the complementary side of a binary market. (Kalshi publishes
only bid ladders, so its YES asks are derived the same way.) Top-of-book fields
are kept for reference and logging only; every price detection uses comes from a
ladder walk. The original payload is preserved in `raw`, and each Market carries
its own observation timestamp, which is what the staleness gate reads: if either
leg is too old the evaluation is **skipped**, not computed, because a fresh book
compared against a stale one prices a phantom.

The tracked match set is refreshed **in process, daily** (`pmarb/refresh.py`,
`MatchSetRefresher`). Most pairs are tied to a single game, so a set discovered
once at startup decays into settled markets that still quote. A cycle re-fetches
both catalogs, re-reads `matches.json`, diffs the pair set, syncs the database
before anything new goes live, writes a close marker for every dropped pair whose
window was still viable, prunes and extends the in-memory state in one pass, and
then mutates the live subscriptions at both venues — no restart. A restart would
also discard the per-pair viability state that closes open windows, which is why
this is in process rather than a cron. It re-reads `matches.json` rather than
re-running the matchers, so `scripts/export_candidates.py` still has to run
first.

## Matching

A matching error is the most dangerous failure in the system: you believe you are
hedged while holding two independent bets that can both lose. So pairs are formed
on **structured identity** wherever both venues encode one, and on text only
where neither does. Five matchers run in the order below, and each is reduced to
a strict 1:1 assignment by `dedupe_one_to_one` with claims carried forward, so a
looser layer can never re-claim a market a more precise one already took.

**structured** (`matching/structured.py`) — head-to-head games and esports
moneylines. Keys on `SportsEvent`: league, start time, the competitor pair, and
which competitor YES pays on. Lexical similarity cannot touch these: Kalshi
phrases a moneyline per team while Poly's question is just "A vs. B" with no YES
semantics, so the YES side lives only in metadata (`yes_sub_title` on Kalshi, the
`long` side of the Poly book). Competitors align by normalized-name subset
("Houston" ⊆ "Houston Astros") with venue team codes as a confirming boost, while
discriminator tokens (`state`, `tech`, directional prefixes) block the subset
where it would be wrong ("Kansas" ⊄ "Kansas St."). An ambiguous alignment is
refused, not guessed: pairing YES(Houston) with YES(Washington) inverts the
hedge. Resolution-date proximity is deliberately not a gate — the venues pad
settlement differently, so game start time is the identity.

**futures** (`matching/futures.py`) — entity outrights. Keys on
`(competition, entity)` plus edition. A golf field is ~150 near-identical "will X
win Y" questions, so text similarity produces a many-to-many mess and
multi-outcome phantoms; grounding each market in its entity fixes the
cardinality. Sub-event selectors ("Stage 9") must match exactly, and the
competition floor is raised above a bare majority because a single shared token
("James", "NASCAR") otherwise pairs different contracts.

**lines** (`matching/lines.py`) — spreads and totals. Keys on league, kind,
**exact line**, game and orientation. Both venues publish the line as a number,
so identity is arithmetic rather than textual; the line is an equality, never a
tolerance, since 45.5 and 46.5 on the same game are different contracts. The hard
part is orientation: Kalshi always writes the laying side, while Poly writes one
market per (game, line) and may quote either the favorite or the underdog —
measured live, roughly half the inventory each way and never both. So on about
half of it Poly's YES is the *complement* of Kalshi's, which a naive pairing would
buy twice while reporting a hedge. Each candidate carries `poly_inverted` and the
collector swaps that leg's ladders; unestablished orientation is refused.

**props** (`matching/props.py`) — player props. Keys on (game, player, stat,
**exact threshold**). Text matching fails because the threshold *is* the
contract: "2+ total bases" reads almost identically to "3+" on the same player in
the same game. Kalshi writes the rung twice (`"3+"` in the subtitle and a
half-point `floor_strike`) and the extractor refuses the market if the two
disagree, turning a wire-format change into a loud failure rather than a wrong
contract. Orientation is never in doubt — both venues write "at least N" as YES,
and each feed refuses anything else.

**lexical** (`matching/matcher.py`) — Jaccard plus character-ratio similarity
over question text, with token blocking to avoid the full cross product. This is
the fallback for genuinely unstructured markets, and it is barred from any market
carrying a structured identity, because those belong to multi-outcome sets where
fuzzy text reliably pairs the wrong outcome. Its pairs are not streamed.

Whole classes are refused rather than approximated: sub-period contracts
(first-half totals, quarter spreads, team totals, first-five-innings) are
allowlisted out on both venues, and doubleheader or series legs that start time
cannot separate are dropped.

A match means "same event, same side" — **not** "same settlement rules". Review
has found pairs matched perfectly on every structured field that still were not
hedges, because the venues settle the tail differently: NPB games tie, NFL
preseason has no overtime, ITF's lowest tiers are full of walkovers, F1 re-points
a fastest-lap market at a substitute driver. `matching/hazards.py` records those
as a lookup keyed on the Kalshi **series** rather than the league, because the
series encodes the tier and market type that carry the signal (ATP main tour and
ITF M15 diverge; F1 race winner and fastest lap diverge). A hazard flags a pair,
it does not reject it, and the table was derived from the labeled negatives, so
treat a new row as a hypothesis. Lines and props have no hazard rows at all.
Resolution divergence stays a human judgement, carried per pair as
`resolution_match` with its review cohort and notes.

## Detection

`detection/spread.py` holds two depth-walking operations, kept separate on
purpose:

- `compute_fill_price(depth, shares)` — fixed size in, average fill price out, or
  None if the book is too thin. This is execution simulation for a size already
  chosen (Phase 2), not detection.
- `max_fillable_size(...)` — the detection workhorse. It walks both legs' ask
  ladders in lockstep, one share of YES against one share of NO, and finds the
  **largest** hedge that still clears the viability gate.

Slippage is applied first and fees second. The walk produces depth-adjusted
average fill prices, and each leg's taker fee is charged on those prices, never
on top of book. Kalshi's fee is a function of the fill price (`0.07·p·(1−p)`) and
is therefore accumulated **per depth level** and divided by the filled size at
exit; because that curve is concave, charging it on the average price would
overstate the blended fee and discard genuinely marginal opportunities.
Polymarket's is flat per category. Both are normalized to **per-share** units
before anything is compared — mixing per-contract with per-100-shares is a
factor-of-100 error that makes the gate either never fire or fire on everything.

The gate at each share count is

```
avg_yes_fill + avg_no_fill + yes_fee_per_share + no_fee_per_share + buffer < 1.00
```

and the greedy stop is exact: average fill prices are non-decreasing as size
grows, since a larger order eats strictly worse levels, so once the legs alone
cost `1 - buffer` no larger size can ever clear. `evaluate_pair` runs both
directions (which venue supplies the YES leg) and keeps the better one, so one
candidate covers both arb directions. When no size clears the fee gate it prices
the hedge at a single contract — the top-of-book case — which charges the loss to
fees rather than to slippage in the funnel.

Three spread fields are recorded, each net of strictly more:
`raw_spread_top_of_book` is before slippage and fees;
`raw_spread_depth_adjusted` is after slippage, before fees; and
`fee_adjusted_spread` is after both, the single viability number and the only
gate the backtest reads. There is deliberately no separate post-fee slippage
field: slippage is already embedded in the fill prices the fees are computed on.

## Persistence and backtest

Every recorded evaluation is dual-written to the JSONL sinks and to Postgres.
`opportunities.jsonl` is the append-only time series and
`opportunities_latest.jsonl` a keyed snapshot of one line per tracked pair;
Postgres carries the same rows plus the latency fields and is the only source the
survival analysis can use.

The write grain is deliberately asymmetric, because a uniform throttle destroys
the one thing the log exists to measure:

- **every** evaluation while a pair is viable, unthrottled — a window opens and
  closes on a book update, so duration resolution should come from the update
  stream rather than from a timer;
- a **close marker** on the first non-viable evaluation after a viable one, which
  is the only thing that pins a window's end (the daily refresh writes its own
  marker with a distinct reason for a pair it stopped watching, since censoring
  and an observed close are opposite facts);
- throttled **edge-change** rows for live games, whose edge actually moves;
- otherwise a periodic **heartbeat**, which is the honest denominator: an absence
  of rows cannot distinguish "watched and never viable" from "never watched", and
  that ambiguity turns every rate into a floor instead of a measurement.
  Heartbeats go to Postgres only, so the JSONL opportunity log stays a log of
  opportunities.

`pmarb/backtest` reads either source plus the current `matches.json`, and drops
samples belonging to pairs the matcher has since retracted from both numerator
and denominator. It writes `backtest_report.json`: the
top-of-book → post-slippage → post-fee funnel overall and per match method, the
spread and fillable-size distributions, window counts and persistence, the
survival curve by elapsed time, detection latency, and the per-run caveats
attached as data rather than as prose someone has to remember. A `--db` replay
must be bounded with `--since`/`--until`, because the observation table pools runs
that differed in coverage and a rate computed across them divides by a
denominator that never existed.

## Running it

```bash
python scripts/verify_auth.py                       # both venues sign correctly
python scripts/export_candidates.py                 # rebuild matches.json (+ review file)
python scripts/seed_db.py                           # sync markets + pairs into Postgres
python -m pmarb.main [DURATION_SECONDS]             # collect (Ctrl+C to stop)
python -m pmarb.backtest.backtest                   # replay the JSONL sinks
python -m pmarb.backtest.backtest --db --since ISO --until ISO   # replay Postgres
docker compose up -d                                # Postgres + collector, restart-on-death
pytest tests && ruff check .
```

Credentials come from a gitignored `.env` (Kalshi signs RSA-PSS, Polymarket US
Ed25519). Design detail lives in module docstrings — `detection/spread.py` for
the depth walk and fee accumulation, `refresh.py` for the refresh ordering, each
matcher for how its identity is pulled off the wire.

**What this is not**: not a profitable trading system — Phase 1 places no orders
— and not evidence that live execution would be one. Execution risk (leg 1
fills, leg 2 moves) is modeled nowhere and would only subtract.
