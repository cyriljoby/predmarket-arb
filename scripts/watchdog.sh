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
# So this judges health by PROGRESS rather than by presence: read the counter
# out of docker logs, compare it with the value from the previous run, and shout
# if it has not advanced. Everything it cannot confirm is reported as its own
# distinct state, never rolled into "healthy" — a watchdog that says nothing
# when it is confused is worse than no watchdog, because it launders an outage
# into silence, which is exactly the failure mode it is here to catch.
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
# How far back to read. Must comfortably exceed the cron interval: the counter
# only prints every 10s, and a window shorter than the gap between runs can miss
# every status line and look like a stall that isn't one.
SINCE="${PMARB_WATCHDOG_SINCE:-10m}"
RESTART="${PMARB_WATCHDOG_RESTART:-0}"

# Plain `[[ ... ]] && VAR=1` would be the last command on its line, so a missing
# flag returns 1 and set -e kills the watchdog before it checks anything.
if [[ "${1:-}" == "--restart" ]]; then
    RESTART=1
fi

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "$(ts) pmarb-watchdog $*"; }

# Any of these is a reason to look, so they share the alert prefix a log grep or
# a mail filter can key on, while the text after it stays specific.
alert() { say "WARNING: $*"; }

maybe_restart() {
    if [[ "$RESTART" != "1" ]]; then
        say "not restarting (alert-only; pass --restart or set PMARB_WATCHDOG_RESTART=1)"
        return 0
    fi
    say "restarting container ${CONTAINER} (--restart given)"
    if docker restart "$CONTAINER" >/dev/null 2>&1; then
        # The counter restarts from zero, so the stored value must go with it or
        # the next run compares a fresh small number against a large old one and
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

# --- pull the newest updates= counter ------------------------------------ #
# `|| true` because docker logs can exit non-zero under set -e, and grep exits 1
# on no match, which is information here rather than an error.
logs="$(docker logs --since "$SINCE" "$CONTAINER" 2>&1 || true)"
# The counter is right-aligned in the status line, so there are spaces between
# the `=` and the digits (`updates=  12345`) — the pattern has to allow them or
# it matches the label and captures no number at all.
current="$(printf '%s\n' "$logs" | grep -oE 'updates=[[:space:]]*[0-9]+' \
    | tail -n 1 | tr -cd '0-9' || true)"

if [[ -z "$current" ]]; then
    # No status line at all in the window. Worse than a stalled counter, not
    # better: the report task itself is not printing, which means the event loop
    # is blocked, stdout got buffered, or the process is hung before it starts
    # reporting. Never treat a missing signal as a passing signal.
    alert "no 'updates=' status line in the last ${SINCE} of ${CONTAINER} logs — report loop is not printing"
    maybe_restart
    exit 1
fi

# --- compare against the previous run ------------------------------------ #
previous=""
if [[ -f "$STATE_FILE" ]]; then
    previous="$(cat "$STATE_FILE" 2>/dev/null || true)"
fi

mkdir -p "$(dirname "$STATE_FILE")"
printf '%s\n' "$current" > "$STATE_FILE"

# A state file that is empty or not a number (truncated write, disk full, an
# earlier version of this script) is treated as no state rather than fed to a
# numeric comparison that would abort the run under set -e.
if [[ ! "$previous" =~ ^[0-9]+$ ]]; then
    # First run, or state cleared by a restart. There is nothing to compare
    # against, so this is explicitly a baseline and not an all-clear.
    say "first run or unusable state file — baselining at updates=${current}, no verdict this cycle"
    exit 0
fi

if [[ "$current" -gt "$previous" ]]; then
    say "ok: updates advanced ${previous} -> ${current}"
    exit 0
fi

if [[ "$current" -lt "$previous" ]]; then
    # Counter went backwards, so the process restarted between checks and its
    # elapsed timer reset. Not wedged, but worth saying out loud — an unexplained
    # restart is how a crash loop announces itself.
    say "counter reset ${previous} -> ${current}: the collector restarted since the last check"
    exit 0
fi

alert "COLLECTOR WEDGED: updates= has not advanced past ${current} since the last check. Container is up and printing, but no book updates are being processed — likely a silently dead subscription or a socket the venue stopped sending on."
maybe_restart
exit 1
