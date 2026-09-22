#!/usr/bin/env bash
# Daily match-set regeneration. Runs from cron on the VPS, once a day.
#
# WHY THIS EXISTS, given the collector already refreshes in process:
# `MatchSetRefresher` (pmarb/refresh.py) re-reads matches.json every day at
# MATCH_REFRESH_HOUR_UTC and mutates both venues' live subscriptions with no
# restart — but it does NOT re-run the matchers. Nothing inside the process ever
# rewrites matches.json, so without something outside it the refresh re-reads
# the same file forever: settled games keep getting dropped and no newly-listed
# market is ever added. 78% of tracked pairs are tied to a single game, so a
# frozen match set bleeds down to nothing within days.
#
# So: regenerate the file, then reseed the database. That is all. NO COLLECTOR
# RESTART IS NEEDED ANY MORE — MatchSetRefresher picks the new file up on its
# next cycle, and a restart would destroy `last_append`, leaving every open
# window unclosed (see pmarb/refresh.py). Regenerating the match set is now the
# ONLY match-set chore cron has left; the only other cron job is watchdog.sh,
# which watches for a wedged collector and is unrelated to this.
#
# Cron (daily, 09:30 UTC — MATCH_REFRESH_HOUR_UTC is 11, so the matcher gets
# ~90 minutes of margin to fetch both catalogs, match, and seed before the
# collector reads the file; if it overruns or fails, the collector simply
# refreshes against yesterday's set, which is a soft failure, not a crash):
#   30 9 * * * /opt/predmarket-arb/scripts/refresh.sh >> /var/log/pmarb-refresh.log 2>&1
#
# HOST VENV, NOT `docker compose run`, even though the image now carries
# scripts/ (see the Dockerfile) and could run these as one-off tasks:
#   - matches.json is bind-mounted into the collector READ-ONLY, deliberately,
#     so a container cannot write the file this script exists to replace;
#   - .dockerignore excludes reviews.json and matches_*.json, and
#     `upsert_match_pairs` writes review fields VERBATIM — a seed_db.py run
#     inside the image would see no reviews.json, silently blanking every human
#     verdict (that has already happened once, to 21 withdrawn verdicts). It
#     would also have nowhere to keep the dated archives;
#   - the staging swap has to be a rename on the same filesystem as the real
#     matches.json, which a container's copied-in layer is not.
# The image's scripts/ copy stays useful for one-off tasks that touch only the
# database (migrations, an ad-hoc reseed) — this one touches host state.

set -euo pipefail

# Derived from the script's own location rather than hardcoded, so a checkout at
# a different path (laptop vs /opt) needs no edit.
REPO="${PMARB_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${PMARB_PYTHON:-$REPO/.venv/bin/python}"
MATCHES="$REPO/matches.json"
REVIEW="$REPO/matches_review.json"
# Repo root by default because that is where the hand-rolled archives already
# live (matches_2026-07-19.json, matches_2026-08-18_prehazard.json) and the
# review loop reads them from there. Overridable because this grows daily now.
ARCHIVE_DIR="${PMARB_REFRESH_ARCHIVE_DIR:-$REPO}"
LOCK_DIR="${PMARB_REFRESH_LOCK:-/var/tmp/pmarb-refresh.lock}"

# --- sanity-gate thresholds ---------------------------------------------- #
# Both catalog fetches are authenticated network calls, and a partial page walk
# or an auth hiccup does not raise — it yields a SMALLER catalog, which matches
# fewer pairs, which is a perfectly valid-looking matches.json that silently
# shrinks the monitored universe. Same silent-degradation class as the
# Polymarket subscription-cap bug that streamed 2,000 of 8,082 markets for two
# hours while looking healthy. A shrunk match set is worse than a stale one:
# stale keeps measuring yesterday's pairs, shrunk stops measuring and leaves no
# trace but a smaller number nobody was watching.
#
# MIN_PAIRS is an absolute floor. The set has only ever grown (960 on Jul 19,
# 3,049 on Aug 18, 7,393 now); 500 is below every set ever generated, so it
# only fires on a genuinely broken fetch, not on a thin holiday slate.
MIN_PAIRS="${PMARB_REFRESH_MIN_PAIRS:-500}"
# MIN_FRACTION is the day-over-day guard, which is the one that actually catches
# a half-fetched catalog: the set is game-heavy so it breathes with the slate,
# but it has never halved overnight, whereas a lost catalog page takes most of
# the pairs with it.
MIN_FRACTION="${PMARB_REFRESH_MIN_FRACTION:-0.5}"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "$(ts) pmarb-refresh $*"; }
# Shares a grep-able prefix with watchdog.sh so one mail filter covers both.
fail() { echo "$(ts) pmarb-refresh WARNING: $*" >&2; exit 1; }

# Two matchers running at once would race the swap and could seed the database
# from one set while installing the other. mkdir is atomic, so it is the lock.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    fail "another refresh holds $LOCK_DIR — refusing to run two matchers at once"
fi

# Staging dir and lock both come down on EVERY exit path, including the sanity
# gate's refusal and any set -e abort, so a failed run leaves nothing behind to
# confuse the next one.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/pmarb-refresh.XXXXXX")"
cleanup() { rm -rf "$STAGE"; rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

[[ -x "$PY" ]] || fail "no interpreter at $PY (set PMARB_PYTHON)"

# Counts both the total and the streamed subset. The collector only subscribes
# the trusted tiers (structured/futures/line/prop), so a fetch failure confined
# to one venue's sports catalog could halve what is actually monitored while the
# total barely moves — gate on both numbers, not just the headline one.
count_pairs() {
    "$PY" - "$1" <<'PYEOF'
import json, sys
TRUSTED = {"structured", "futures", "line", "prop"}
with open(sys.argv[1]) as f:
    m = json.load(f)
if not isinstance(m, list):
    raise SystemExit("not a JSON list")
print(len(m), sum(1 for c in m if c.get("match_method") in TRUSTED))
PYEOF
}

# --- how big is the set we are replacing? --------------------------------- #
# A missing or truncated current file is not an error here — it is exactly the
# state this script has to be able to recover from, so the day-over-day gate is
# skipped and the absolute floor carries the decision alone. Said out loud,
# because "the ratio gate did not run" must never pass silently as "it passed".
cur_total=0
cur_trusted=0
have_current=0
salvage_corrupt=0
if [[ -f "$MATCHES" ]]; then
    if read -r cur_total cur_trusted < <(count_pairs "$MATCHES" 2>/dev/null); then
        have_current=1
        say "current match set: ${cur_total} pairs (${cur_trusted} streamed)"
    else
        # Kept for forensics rather than silently overwritten: an unparseable
        # match set means something crashed mid-write, and the truncation point
        # is the only evidence of where.
        salvage_corrupt=1
        say "current $MATCHES is missing or unparseable — ratio gate SKIPPED, absolute floor only"
    fi
else
    say "no current $MATCHES — first run or recovery; ratio gate SKIPPED, absolute floor only"
fi

# --- regenerate, in staging ---------------------------------------------- #
# export_candidates.py writes to paths RELATIVE TO ITS CWD: write_matches()
# defaults to config.MATCH_LOG_PATH ("matches.json") and the review file is a
# bare "matches_review.json". There is no output-path argument, so the only way
# to stage is to run it from the staging directory. It imports pmarb from the
# editable install and credentials.py loads .env from the package's own repo
# root, so neither import nor auth cares about the cwd.
#
# In place would be the bug: write_matches opens the target "w" and streams JSON
# into it, so a crash or an OOM kill mid-write leaves TRUNCATED JSON, and
# main.py / MatchSetRefresher do a bare json.load on it. Under
# `restart: unless-stopped` a truncated match set is not a bad refresh, it is a
# crash-looping container. Staging plus a rename means the collector only ever
# sees a whole file.
say "running export_candidates.py in $STAGE"
if ! (cd "$STAGE" && "$PY" "$REPO/scripts/export_candidates.py"); then
    fail "export_candidates.py FAILED — keeping the existing match set untouched"
fi

new_matches="$STAGE/matches.json"
new_review="$STAGE/matches_review.json"
[[ -s "$new_matches" ]] || fail "export_candidates.py wrote no $new_matches"
[[ -s "$new_review" ]] || fail "export_candidates.py wrote no $new_review"

if ! read -r new_total new_trusted < <(count_pairs "$new_matches"); then
    fail "new match set is not parseable JSON — refusing the swap"
fi
say "new match set: ${new_total} pairs (${new_trusted} streamed)"

# --- sanity gate ---------------------------------------------------------- #
if (( new_total < MIN_PAIRS )); then
    fail "REFUSING SWAP: new set has ${new_total} pairs, below the absolute floor of ${MIN_PAIRS} — a catalog fetch almost certainly came back partial. Existing match set left in place."
fi
if (( have_current == 1 )); then
    # Integer floors computed with the interpreter already in hand rather than
    # bc, which is not installed everywhere.
    floor_total="$("$PY" -c "import math,sys; print(math.floor(float(sys.argv[1])*int(sys.argv[2])))" "$MIN_FRACTION" "$cur_total")"
    floor_trusted="$("$PY" -c "import math,sys; print(math.floor(float(sys.argv[1])*int(sys.argv[2])))" "$MIN_FRACTION" "$cur_trusted")"
    if (( new_total < floor_total )); then
        fail "REFUSING SWAP: new set has ${new_total} pairs, under ${MIN_FRACTION} of the current ${cur_total} (floor ${floor_total}) — this is a shrink, not a slate change. Existing match set left in place."
    fi
    if (( new_trusted < floor_trusted )); then
        fail "REFUSING SWAP: new set streams ${new_trusted} pairs, under ${MIN_FRACTION} of the current ${cur_trusted} (floor ${floor_trusted}) — one venue's catalog likely came back short even though the total held up. Existing match set left in place."
    fi
fi
say "sanity gate passed"

# --- archive, then swap --------------------------------------------------- #
# Provenance is not housekeeping here: reviews.json labels are joined to pairs
# by VENUE MARKET ID, so to re-read a verdict you need the match set it was
# written against. The repo already keeps these by hand
# (matches_2026-07-19.json, matches_2026-08-18_prehazard.json); this just stops
# it being by hand. UTC date stamp because every other timestamp in this system
# is UTC, and a second run on the same day gets a time-stamped name rather than
# overwriting the morning's evidence.
stamp="$(date -u +%Y-%m-%d)"
if [[ -e "$ARCHIVE_DIR/matches_$stamp.json" ]]; then
    stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
fi
if (( have_current == 1 )); then
    # Copy, not move: until the rename below lands, the collector must still
    # have a complete file to read.
    cp "$MATCHES" "$ARCHIVE_DIR/matches_$stamp.json"
    say "archived previous match set -> $ARCHIVE_DIR/matches_$stamp.json"
fi
if (( salvage_corrupt == 1 )); then
    cp "$MATCHES" "$ARCHIVE_DIR/matches_$stamp.corrupt.json"
    say "kept the unparseable previous file -> $ARCHIVE_DIR/matches_$stamp.corrupt.json"
fi

# Same filesystem as the target, so this is an atomic rename and not a copy the
# collector could catch half-written. mktemp's TMPDIR may be on another mount,
# hence the hop through a sibling of the real file first.
cp "$new_matches" "$MATCHES.new"
mv "$MATCHES.new" "$MATCHES"
say "installed new match set ($new_total pairs)"

# The review file is the LLM/manual review loop's input, so the live copy is
# replaced and the dated copy kept beside its match set.
cp "$new_review" "$ARCHIVE_DIR/matches_review_$stamp.json"
cp "$new_review" "$REVIEW.new"
mv "$REVIEW.new" "$REVIEW"
say "installed matches_review.json (archived as $ARCHIVE_DIR/matches_review_$stamp.json)"

# --- reseed the database -------------------------------------------------- #
# AFTER the swap because seed_db.py reads config.MATCH_LOG_PATH — it seeds
# whatever matches.json currently says — and BEFORE MatchSetRefresher's cycle
# because the writer resolves pairs through `pair_ids(conn)`: a pair present in
# matches.json with no `match_pair` row has its observations SILENTLY DROPPED,
# which reads later as "monitored and never viable" rather than "never written".
#
# Must run from the repo root: seed_db.py opens MATCH_LOG_PATH and reviews.json
# by relative path, and reviews.json is the file whose verdicts
# upsert_match_pairs copies verbatim — run it anywhere else and it seeds with
# zero reviews and blanks every label.
#
# Belt and braces, deliberately: MatchSetRefresher step 3 does its own
# upsert_markets/upsert_match_pairs before touching subscriptions, so on a
# healthy collector this is redundant. It is here for the cases where it isn't
# — collector down, refresh cycle errored, or a backtest reading the database
# before the collector's next 11:00 UTC cycle.
say "running seed_db.py"
if ! (cd "$REPO" && "$PY" "$REPO/scripts/seed_db.py"); then
    fail "seed_db.py FAILED after the swap — matches.json IS the new set but the database may be missing match_pair rows for new pairs, whose observations will be dropped until the collector's own refresh syncs them. Rerun: (cd $REPO && $PY scripts/seed_db.py)"
fi

say "ok: refresh complete — no restart needed, MatchSetRefresher picks it up at 11:00 UTC (config.MATCH_REFRESH_HOUR_UTC)"
