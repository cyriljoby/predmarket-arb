#!/usr/bin/env bash
# Wedged-collector watchdog. Runs from cron on the VPS every few minutes.
#
# WHY THIS EXISTS, given `restart: unless-stopped` is already set:
# Docker's restart policy only notices a process that EXITS. The outages that
# have actually cost this project data did not exit. A Polymarket
# subscription-cap bug once ran for two hours looking perfectly healthy while
# streaming 2,000 of 8,082 markets; the venue has also stopped sending on a
# socket it never closed, which the process reads as a quiet market rather than
# a fault. In both cases the container is up, the healthcheck (if there were
# one) passes, and the restart policy has nothing to fire on — the only visible
# symptom is that the `updates=` counter on the 10s status line stops moving.
#
# WHY IT WATCHES MORE THAN `updates=`:
# It watched only `updates=` for its first version, and that let a 13.8-day
# outage through. The collector was alive, its sockets were healthy and
# `updates=` was climbing at ~55/s, while `samples=`, `windows=`, `db=` and
# `tracked=` had all been frozen at identical values since 2026-09-08 11:52 UTC
# and both JSONL sinks stopped at that timestamp. matches.json had gone stale,
# so nearly every tracked pair had settled: books kept arriving, and no
# evaluation produced a row. INGESTION LIVENESS IS NOT MEASUREMENT LIVENESS,
# and measurement is the only thing this collector exists to do — so the
# counters that prove a row was written are checked alongside the one that
# proves bytes arrived, and the two failures are reported as different faults
# because they have different fixes.
#
# So this judges health by PROGRESS rather than by presence: read the counters
# out of docker logs, compare them with the values from the previous run, and
# shout if they have not advanced. Everything it cannot confirm is reported as
# its own distinct state, never rolled into "healthy" — a watchdog that says
# nothing when it is confused is worse than no watchdog, because it launders an
# outage into silence, which is exactly the failure mode it is here to catch.
#
# Restarting is opt-in (--restart, or PMARB_WATCHDOG_RESTART=1) and off by
# default: a restart drops both order books and re-fetches both catalogs, so a
# false positive — a genuinely quiet few minutes, a clock skew, a truncated log
# window — costs real observations. Alerting a human is cheap; restarting is
# not. Output goes to stdout so cron mails it or a redirect files it.
#
# Cron (every 5 minutes, alert only):
#   */5 * * * * /opt/predmarket-arb/scripts/watchdog.sh >> /var/log/pmarb-watchdog.log 2>&1

set -euo pipefail

CONTAINER="${PMARB_CONTAINER:-pmarb-collector}"
STATE_FILE="${PMARB_WATCHDOG_STATE:-/var/tmp/pmarb-watchdog.state}"
# How far back to read. Must comfortably exceed the cron interval: the counters
# only print every 10s, and a window shorter than the gap between runs can miss
# every status line and look like a stall that isn't one.
SINCE="${PMARB_WATCHDOG_SINCE:-10m}"
RESTART="${PMARB_WATCHDOG_RESTART:-0}"
# Bumped whenever the state file's shape changes, and checked on read. The first
# version wrote a bare number; a version tag is what lets this one recognise
# that file as unusable and re-baseline instead of parsing the number as
# whichever field happens to be read first.
STATE_VERSION=2

# Plain `[[ ... ]] && VAR=1` would be the last command on its line, so a missing
# flag returns 1 and set -e kills the watchdog before it checks anything.
if [[ "${1:-}" == "--restart" ]]; then
    RESTART=1
fi

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_epoch() { date -u +%s; }
say() { echo "$(ts) pmarb-watchdog $*"; }

# Any of these is a reason to look, so they share the alert prefix a log grep or
# a mail filter can key on, while the text after it stays specific.
alert() { say "WARNING: $*"; }

# Seconds as something a human reads at 3am without dividing by 86400.
human_age() {
    local s="$1"
    if (( s < 3600 )); then
        printf '%dm' "$(( s / 60 ))"
    elif (( s < 172800 )); then
        printf '%dh%dm' "$(( s / 3600 ))" "$(( (s % 3600) / 60 ))"
    else
        printf '%dd%dh' "$(( s / 86400 ))" "$(( (s % 86400) / 3600 ))"
    fi
}

maybe_restart() {
    if [[ "$RESTART" != "1" ]]; then
        say "not restarting (alert-only; pass --restart or set PMARB_WATCHDOG_RESTART=1)"
        return 0
    fi
    say "restarting container ${CONTAINER} (--restart given)"
    if docker restart "$CONTAINER" >/dev/null 2>&1; then
        # The counters restart from zero, so the stored values must go with them
        # or the next run compares fresh small numbers against large old ones and
        # reports a stall that is really a restart.
        rm -f "$STATE_FILE"
        say "restart issued; state cleared so the next run re-baselines"
    else
        alert "docker restart ${CONTAINER} FAILED — needs a human"
    fi
}

# --- is the container even running? -------------------------------------- #
# Distinct from wedged: a container that is down is the restart policy's job and
# its failure to come back is a different problem with a different fix.
running="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$running" != "true" ]]; then
    if [[ -z "$running" ]]; then
        alert "container ${CONTAINER} does not exist — never deployed, or removed"
    else
        alert "container ${CONTAINER} exists but is NOT running (restart policy did not bring it back)"
    fi
    exit 1
fi

# --- pull the newest status line ----------------------------------------- #
# `|| true` because docker logs can exit non-zero under set -e, and grep exits 1
# on no match, which is information here rather than an error.
logs="$(docker logs --since "$SINCE" "$CONTAINER" 2>&1 || true)"
# One line, then every counter off THAT line. Grepping each counter over the
# whole window independently would happily pair `updates=` from the newest line
# with `samples=` from an older one, which is precisely the comparison this
# script exists to get right.
status="$(printf '%s\n' "$logs" | grep -E 'updates=[[:space:]]*[0-9]+' | tail -n 1 || true)"

# The counters are width-aligned in the status line (`updates={...:>7}`), so
# there are spaces between the `=` and the digits (`updates=  12345`) — the
# pattern has to allow them or it matches the label and captures no number at
# all, which is the bug the first version shipped with. Empty output means the
# field was ABSENT, which for `db=` is a real and different state from frozen.
field() {
    printf '%s\n' "$2" \
        | grep -oE "(^|[[:space:]])$1=[[:space:]]*[0-9]+" \
        | tail -n 1 | tr -cd '0-9' || true
}

cur_updates="$(field updates "$status")"
cur_samples="$(field samples "$status")"
# `db=` is printed only when the Postgres writer exists, so its absence means
# the database is unreachable (or disabled), NOT that writing has stopped.
cur_db="$(field db "$status")"

if [[ -z "$cur_updates" || -z "$cur_samples" ]]; then
    # No status line at all in the window. Worse than a stalled counter, not
    # better: the report task itself is not printing, which means the event loop
    # is blocked, stdout got buffered, or the process is hung before it starts
    # reporting. Never treat a missing signal as a passing signal.
    alert "no usable status line in the last ${SINCE} of ${CONTAINER} logs — report loop is not printing"
    maybe_restart
    exit 1
fi

# --- which counter is the honest measurement signal? --------------------- #
# `db=` is `writer.written`, and the write grain sends a HEARTBEAT row for every
# tracked pair every HEARTBEAT_SECONDS (300) whether or not anything is viable
# (pmarb/main.py sample_reason + the writer.submit call), so on a healthy
# collector with even one tracked pair it advances regardless of market
# conditions. `samples=` is `event_log.count`, and heartbeats are deliberately
# EXCLUDED from the JSONL sinks — so a genuinely quiet stretch with nothing
# viable and no live structured game can freeze `samples=` legitimately, which
# makes it the fallback rather than the primary. `windows=` is weaker still
# (only a positive EDGE opens one) and is never alerted on at all.
if [[ -n "$cur_db" ]]; then
    signal="db"
else
    signal="samples"
fi

# --- compare against the previous run ------------------------------------ #
prev_version="" prev_updates="" prev_samples="" prev_db="" prev_frozen_since=""
if [[ -f "$STATE_FILE" ]]; then
    # Read as key=value rather than by position, and ignore anything unexpected,
    # so a partially written file degrades into "no state" instead of into a
    # wrong comparison.
    while IFS='=' read -r key value; do
        case "$key" in
            version)      prev_version="$value" ;;
            updates)      prev_updates="$value" ;;
            samples)      prev_samples="$value" ;;
            db)           prev_db="$value" ;;
            frozen_since) prev_frozen_since="$value" ;;
        esac
    done < "$STATE_FILE"
fi

# A state file that is empty, of an older shape (the first version held one bare
# number), or not numeric where numbers belong is treated as no state rather
# than fed to a numeric comparison that would abort the run under set -e.
usable_state=1
if [[ "$prev_version" != "$STATE_VERSION" ]]; then
    usable_state=0
elif [[ ! "$prev_updates" =~ ^[0-9]+$ || ! "$prev_samples" =~ ^[0-9]+$ ]]; then
    usable_state=0
elif [[ -n "$prev_db" && ! "$prev_db" =~ ^[0-9]+$ ]]; then
    usable_state=0
fi

now="$(now_epoch)"

# The signal actually COMPARED can differ from the primary one: `db=` is only
# comparable when the previous run also saw it. A `db=` that has just appeared
# (Postgres came back, or the collector was restarted with it enabled) has no
# earlier value, and comparing it against nothing would read as a fresh zero.
cmp_signal="$signal"
if [[ "$signal" == "db" && ! "$prev_db" =~ ^[0-9]+$ ]]; then
    cmp_signal="samples"
fi
if [[ "$cmp_signal" == "db" ]]; then
    cur_cmp="$cur_db" prev_cmp="$prev_db"
else
    cur_cmp="$cur_samples" prev_cmp="$prev_samples"
fi

# How long the compared measurement signal has been stuck. Carried forward while
# it is stuck and reset the moment it moves, so the alert can say "frozen for
# 13d19h" instead of "frozen since the last check" — the duration is what tells a
# human whether this is a blip or a fortnight of lost data.
if [[ "$usable_state" == "1" && "$prev_frozen_since" =~ ^[0-9]+$ ]]; then
    frozen_since="$prev_frozen_since"
else
    frozen_since="$now"
fi
if [[ "$usable_state" == "1" && "$prev_cmp" =~ ^[0-9]+$ && "$cur_cmp" != "$prev_cmp" ]]; then
    frozen_since="$now"
fi

write_state() {
    mkdir -p "$(dirname "$STATE_FILE")"
    {
        printf 'version=%s\n' "$STATE_VERSION"
        printf 'checked_at=%s\n' "$now"
        printf 'updates=%s\n' "$cur_updates"
        printf 'samples=%s\n' "$cur_samples"
        # Written only when present, so "absent" survives the round trip as an
        # absent key rather than as a zero that would later read as frozen.
        if [[ -n "$cur_db" ]]; then
            printf 'db=%s\n' "$cur_db"
        fi
        printf 'frozen_since=%s\n' "$frozen_since"
    } > "$STATE_FILE"
}
write_state

if [[ "$usable_state" != "1" ]]; then
    # First run, state cleared by a restart, or a state file from an older
    # version of this script. There is nothing to compare against, so this is
    # explicitly a baseline and not an all-clear.
    say "first run or unusable state file — baselining at updates=${cur_updates} samples=${cur_samples}${cur_db:+ db=${cur_db}}, no verdict this cycle"
    exit 0
fi

if [[ "$cur_updates" -lt "$prev_updates" || "$cur_cmp" -lt "$prev_cmp" ]]; then
    # A counter went backwards, so the process restarted between checks and its
    # elapsed timer reset. Not wedged, but worth saying out loud — an unexplained
    # restart is how a crash loop announces itself. The state written above is
    # already the new baseline, so the next run compares like with like.
    say "counter reset (updates ${prev_updates} -> ${cur_updates}, ${cmp_signal} ${prev_cmp} -> ${cur_cmp}): the collector restarted since the last check, re-baselined"
    exit 0
fi

if [[ "$cur_updates" == "$prev_updates" ]]; then
    # Ingestion itself has stopped. Checked before the measurement signal
    # because it is upstream of it: with no book updates arriving there is
    # nothing to evaluate, so a frozen measurement signal here is a symptom and
    # reporting it as the fault would point the reader at the wrong layer.
    alert "COLLECTOR WEDGED: updates= has not advanced past ${cur_updates} since the last check. Container is up and printing, but no book updates are being processed — likely a silently dead subscription or a socket the venue stopped sending on."
    maybe_restart
    exit 1
fi

# `db=` was there last time and is gone now: the writer is absent, so Postgres
# is unreachable or the collector came up without it. Its own fault, reported
# before the frozen check so it is never silently reinterpreted as a stall.
if [[ -z "$cur_db" && -n "$prev_db" ]]; then
    alert "db= has DISAPPEARED from the status line (was ${prev_db}) — the Postgres writer is gone, so observations are no longer being persisted. Falling back to samples= as the measurement signal this cycle."
fi

if [[ "$cur_cmp" == "$prev_cmp" ]]; then
    # THE 13.8-DAY FAILURE. Bytes are flowing and nothing is being measured.
    # The usual cause is a stale matches.json: every tracked pair has settled,
    # so books arrive, get evaluated against nothing, and write no row.
    #
    # Deliberately NOT wired to maybe_restart. A restart re-reads the same stale
    # matches.json (pmarb/main.py load_trusted_matches at startup), so it throws
    # away both order books and fixes nothing; the remedy is scripts/refresh.sh
    # regenerating the match set. Restarting a collector that is ingesting fine
    # would make this alert strictly more expensive than the outage it reports.
    frozen_for="$(( now - frozen_since ))"
    frozen_at="$(date -u -r "$frozen_since" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -d "@${frozen_since}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || echo "$frozen_since")"
    # Only `db=` is unconditional, because heartbeat rows go to Postgres only.
    # When it is unavailable the verdict rests on `samples=`, which a quiet
    # market can freeze on its own — still reported, because silence is the
    # failure mode this script exists to prevent, but flagged as the weaker
    # call so nobody tears up a healthy collector over a slow Sunday.
    caveat=""
    if [[ "$cmp_signal" != "db" ]]; then
        caveat=" NOTE: no db= to check, and samples= excludes heartbeat rows, so a genuinely quiet market can freeze it too — confirm against matches.json age before acting."
    fi
    alert "INGESTING BUT NOT MEASURING: updates= is advancing (${prev_updates} -> ${cur_updates}) but ${cmp_signal}= has been stuck at ${cur_cmp} for $(human_age "$frozen_for") (since ${frozen_at}). Books are arriving and no evaluation is producing a row. SUSPECT A STALE matches.json — if the tracked pairs have settled the collector will look healthy forever. Check the age of matches.json and run scripts/refresh.sh; a container restart will NOT fix this.${caveat}"
    say "not restarting for this fault regardless of --restart: a restart re-reads the same stale match set"
    exit 1
fi

# `samples=` standing still while the heartbeat-backed signal advances is a
# normal quiet market (nothing viable, no live game moving), not a fault — said
# out loud so the ok line never hides which counters actually moved.
if [[ "$cmp_signal" == "db" && "$cur_samples" == "$prev_samples" ]]; then
    say "ok: updates ${prev_updates} -> ${cur_updates}, db ${prev_db} -> ${cur_db} (samples= unchanged at ${cur_samples}: no viable window or live edge change this window, which a quiet market does legitimately)"
    exit 0
fi

if [[ "$cmp_signal" == "db" ]]; then
    say "ok: updates ${prev_updates} -> ${cur_updates}, samples ${prev_samples} -> ${cur_samples}, db ${prev_db} -> ${cur_db}"
else
    # Only reachable with no comparable `db=`, so `samples=` carried the verdict
    # on its own — noted because it is the weaker of the two signals.
    say "ok: updates ${prev_updates} -> ${cur_updates}, samples ${prev_samples} -> ${cur_samples} (no comparable db= this cycle; samples= was the measurement signal)"
fi
exit 0
