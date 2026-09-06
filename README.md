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
                         ├─ StructuredMatcher   games:     league + start + YES side
                         ├─ FuturesMatcher      outrights: competition × entity
                         ├─ LineMatcher         spreads/totals: game × line
                         ├─ PropMatcher         props:     game × player × stat × rung
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

### Discovery — four structured matchers, then text

A matching error is the most dangerous failure in the system: you believe you
are hedged when you hold two independent bets that can both lose. So markets are
paired on **structured identity** wherever both venues encode one, and on text
only where neither does.

| matcher | handles | identity key | pairs |
|---|---|---|---|
| `matching/structured.py` | head-to-head games | league, start time, competitor pair, which competitor YES pays on | ~430 |
| `matching/futures.py` | entity outrights | `(competition, entity)`, sub-event selectors ("Stage 9") exact | ~1,050 |
| `matching/lines.py` | spreads and totals | league, kind, **exact line**, game, orientation | ~1,160 |
| `matching/props.py` | player props | game, player, stat, **exact threshold** | ~1,210 |
| `matching/matcher.py` | everything unstructured | Jaccard + character ratio over question text | 0 today |

Counts are one live snapshot and move constantly — game-day markets are listed
days ahead and halted at kickoff, so a fetch six hours later is legitimately
different. The stable figures are the verified ones below.

#### Where each fact comes from

The two venues put their structure in different places, and this table is the
whole translation. **Polymarket states its taxonomy in an enum** (`marketType`,
6 values; `sportsMarketType`, ~144 values shaped `sport_entity_period_stat`).
**Kalshi has no such field** — 4,070 opaque series prefixes (`KXMLBKS`), with
the readable concept in the event *title* instead.

| fact | Kalshi | Polymarket US |
|---|---|---|
| market family | series ticker prefix (hand-mapped) | `sportsMarketType` |
| game (teams) | enclosing event title, "A vs B: Stat" | `marketSides[].team`, else description prose |
| game start | event-ticker segment, **US Eastern** | `gameStartTime` (UTC) |
| competitor (moneyline) | `yes_sub_title` | `team.name` + `team.safeName` |
| outright entity | `yes_sub_title` | `title` |
| line (spread/total) | `floor_strike`, `strike_type: greater` | `line` |
| prop player | `yes_sub_title` before ":" | `title` |
| prop threshold | `"N+"` in `yes_sub_title` (= `floor_strike` + 0.5) | `line` (gte) |

Two conventions bite here. Kalshi writes a prop rung twice — `"3+"` and
`floor_strike: 2.5` — and the extractor reads **both and refuses the market if
they disagree**, since one number written two ways is a free integrity check.
And Poly's `team.name` is the MASCOT in college football ("Bulldogs") while
`safeName` is the school ("The Citadel"); Kalshi names the school, so the two
are joined only when neither contains the other. Before that fix, 176 CFB games
matched nothing at all.

#### Orientation — why spreads need a flag

Kalshi always writes the laying side ("Vanderbilt wins by over 41.5"). Poly
writes ONE market per (game, line) and may quote either side: measured live,
4,046 groups were favorite-side and 4,477 underdog-side, **never both**. So on
roughly half the inventory Poly's YES is the *complement* of Kalshi's YES.

The detector hedges YES against NO. Handed a complementary pair unswapped, it
would buy the same event on both venues and price a guaranteed profit that does
not exist. So each candidate carries `poly_inverted`, and the collector swaps
that leg's ladders (`main.oriented`). The valid combinations are exactly:

| Poly YES | Kalshi market | result |
|---|---|---|
| team at −L | that team's | direct |
| team at +L | the **opponent's** | inverted |
| team at +L | that team's | **refused** — shares a team and a number, different bet |

Props need no such flag: both venues write "at least N" as YES, and each feed
refuses anything else rather than assuming.

#### What each layer refuses

Refusals are load-bearing — an unmatched pair costs coverage, a wrongly matched
one costs money.

- **Sub-periods.** First-half totals, quarter spreads, team totals and
  first-five-innings markets are separate contracts. Poly is filtered by an
  allowlist of six `full_game` types (`football_team_points_full_game_total`
  contains "full_game" but is a *team* total, so it is excluded); Kalshi by an
  allowlist of full-game series.
- **Adjacent rungs.** A total at 45.5 and one at 46.5, or a prop at 2+ and 3+,
  are different contracts. The line/threshold is part of the key, never a
  tolerance.
- **Ambiguous games.** Same teams, same line, twice inside the window (a series
  or doubleheader) resolves by closest start; a genuine tie is refused.
- **Unknown orientation or unreadable sides** are dropped rather than guessed.
- **Text matching on structured markets.** The lexical matcher is barred from
  any market carrying a structured identity: those belong to multi-outcome sets,
  where fuzzy text reliably pairs the wrong outcome. It currently claims nothing
  — every market it would reach is now typed.

Each layer is then reduced to a strict 1:1 assignment by `dedupe_one_to_one`,
with claims carried forward so a looser layer can never re-claim a market a more
precise one already took.

Every pair lands in `matches.json` carrying `match_method`, `poly_inverted`,
`settlement_hazards`, and its review lineage (`resolution_match`, cohort,
reviewer) — so what the collector streams, and why, is auditable from one file.

#### Verified coverage

Measured against a frozen catalog, checking each pair independently of the
matcher's own logic (identity triple equal, competitors aligned, orientation
correct):

| layer | verified pairs | violations |
|---|---|---|
| lines (spreads + totals) | **1,520** | 0 |
| props | **1,343** | 0 |

Props were hidden for months by a classification bug: Kalshi phrases them
"Cal Raleigh: 2+", which reads as an entity, so all 6,331 were typed as
outrights and offered to the futures matcher — where they matched nothing,
because Poly's props are not outright-shaped. The block looked empty from both
directions.

#### Settlement hazards

A match means "same event, same side" — **not** "same settlement rules." Of 47
pairs that review labeled NOT a resolution match, 20 were matched perfectly and
still were not hedges: NPB games end in ties, NFL preseason has no overtime,
ITF's lowest tiers are full of walkovers, F1 re-points a fastest-lap market at a
substitute driver.

`matching/hazards.py` records those as a lookup keyed on the Kalshi **series**,
not the league — the tier distinction carries the whole signal:

| keyed on | false flags across 243 reviewed-true pairs |
|---|---|
| series | **0** |
| "tennis" | 8 (ATP/WTA reviewed true, ITF M15/W15 reviewed false) |
| "f1" | 10 (race winner true, fastest lap false) |

The table was derived *from* those negatives, so 0 is a lower bound on its
error, not a measurement of it. It flags ~4% of pairs today. **Lines and props
have no hazard rows at all** — nobody has read the two venues' spread, total and
prop rules side by side. Poly's touchdown props say "excluding passing
touchdowns"; whether Kalshi's `KXNFLTD` agrees is unchecked.

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

## Key findings: wire realities and bugs surfaced

Most of the work in this project has not been algorithmic. It has been finding
out what the two venues actually send, and catching the failures that look like
nothing. The pattern worth naming: **the dangerous bugs here are all silent** —
a dropped subscription, a mis-classified market, an inverted hedge and an idle
market all produce the same thing in a log, which is nothing.

Recorded so the next person does not rediscover them by accident.

### Silent losses

- **Polymarket caps subscriptions at 2,000 per connection.** The 2,001st is
  refused with `{"error": "max subscriptions per connection reached"}`; the
  socket stays healthy and the extra batches are simply never fed. Because the
  read loop only looked at `marketData` frames, a run subscribing to 8,082
  markets streamed 2,000 of them and reported nothing wrong for 2.5 hours.
  Fixed by sharding across connections (1,900 each) and by logging error
  frames. **Note the 30-day run streamed "~2,040 simultaneous books"** — close
  enough to the cap that its coverage may have been truncated too, which would
  make its denominators floors rather than measurements.

- **Kalshi player props were classified as outrights.** Their subject reads
  "Cal Raleigh: 2+", which the entity extractor accepted, so all 6,331 were
  offered to the futures matcher — where they matched nothing, because Poly's
  props are not outright-shaped. The largest matchable block on either venue
  looked empty from both directions for months.

- **Polymarket names college-football teams by MASCOT** (`team.name` =
  "Bulldogs") while the school is in `safeName` ("The Citadel"). Kalshi names
  the school. Zero shared tokens, so 176 CFB games matched nothing — silently,
  because an unmatched pair writes no row anywhere. Fixing it added 148 pairs.

- **A season written as a range left a stray number.** "2026-27" stripped to
  "27", which the futures matcher reads as a sub-event selector ("Stage 9"),
  hard-zeroing the score against any competition without the same stray digits.
  It rejected correct pairs across every season-range sport.

### Things that would have priced a phantom hedge

- **Half of Polymarket's spread inventory is the opposite side.** Kalshi always
  writes the laying side ("Vanderbilt wins by over 41.5"); Poly writes one
  market per (game, line) and quotes either side — 4,046 groups favorite-side,
  4,477 underdog-side, never both. So on ~half the inventory Poly's YES is the
  COMPLEMENT of Kalshi's YES, and pairing them naively buys the same event on
  both venues while reporting a hedge. Carried as `poly_inverted`.

- **Sub-period contracts look identical to full-game ones.** First-half totals,
  quarter spreads, team totals, first-five-innings markets. Poly's
  `football_team_points_full_game_total` even contains "full_game" and is a
  per-TEAM total. Both sides are now allowlisted rather than pattern-matched.

- **"Kansas" is a subset of "Kansas St." and they are different schools.** The
  name-subset rule that correctly pairs "Houston" with "Houston Astros" made
  Kansas State the highest-edge "arb" in one match set. Discriminator tokens
  (`state`, `tech`, `saint`, directional prefixes) now refuse the subset.

- **20 of 47 review-rejected pairs were matched perfectly.** Same game, same
  side, start times to the minute — and still not hedges, because NPB games
  tie, NFL preseason has no overtime, ITF walkovers settle differently, and F1
  re-points a fastest-lap market at a substitute driver. Matching quality
  cannot fix these; see `matching/hazards.py`.

### Encoding traps

- **Kalshi writes a prop threshold twice** — `"15+"` in `yes_sub_title` and
  `floor_strike: 14.5` (half-point, `strike_type: greater`). Poly writes
  `line: 15`, gte. One number, three encodings. The extractor reads both Kalshi
  forms and refuses the market if they disagree, which turns a wire-format
  change into a loud failure instead of a wrong contract.

- **Venue ID abbreviations do not correspond.** Kalshi's `26SEP12DELVAN`
  against Poly's `boscol-cin`: an early overlap estimate that compared them was
  measuring abbreviation schemes, not games. Matching goes through parsed
  fields, never through ids.

- **Kalshi encodes game times in US Eastern** inside the event ticker; Poly's
  `gameStartTime` is UTC. Weekly sports omit the time entirely and parse to
  midnight Eastern, which is why the start-time window is 30h rather than tight.

- **Settlement dates are padded differently** (Kalshi ~+3d, Poly ~+14d after a
  game), so resolution-date proximity must NOT gate game matches. Venues also
  AMEND them — a postponed game is rewritten — which is why observations
  snapshot their own copy instead of joining `market` later.

- **Kalshi's `rules_primary` is sometimes an unfilled template**
  (`"above || Count || by || Date ||"`), so venue rules text cannot be relied
  on for settlement comparison.

### Operational


- **Polymarket halts and re-lists markets intraday.** The catalog swung
  63,530 → 48,718 markets within hours, and NFL player props went
  `MARKET_STATUS_HALTED` five days before kickoff. Match counts move between
  runs for this reason, not because the matchers are unstable — quote the
  frozen-catalog verifications instead.

- **A loud failure is worth keeping loud.** Seeding rejected the first `line`
  pair outright because `match_pair`'s CHECK constraint predated the new
  matchers. That is the constraint working: had it been widened to any text,
  6,100 pairs would have been dropped silently.

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
pytest tests                                     # 265 tests
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
