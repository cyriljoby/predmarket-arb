"""Central configuration: fee constants, thresholds, paths.

Single source of truth for tunable values so no magic numbers hide in logic.
"""

# Market Matching
# Min string similarity to flag a market pair. Tuned by hand; only the lexical
# matcher reads it (structured and futures score on their own scales).
MATCH_THRESHOLD = 0.75
RESOLUTION_DATE_TOLERANCE_DAYS = 10  # resolution dates must fall within this window
# Futures/outright matching (structured). Venues pad outright resolution dates
# more loosely than games, so a wider window; season disambiguation still leans
# on it because competition strings drop the year.
FUTURES_DATE_TOLERANCE_DAYS = 30
FUTURES_ENTITY_MIN = 0.6         # min entity-name score (reuses competitor_score)
# Min normalized-competition overlap. Raised 0.65 -> 0.75 once stated-year
# matching stopped the date gate from masking weak competition scores: at 0.65 a
# few shared generic tokens paired different contracts — "Pro Basketball: Best
# Regular Season Record" with "Pro Football Best Regular Season Record" (0.71),
# "NASCAR Cup Series Regular Season Champion" with "NASCAR Cup Series Champion"
# (0.67), and every "qualify for the ACC Championship" with "advances to the CFP
# National Championship" (0.6667). Genuine same-competition pairs score 0.75-1.0.
FUTURES_COMPETITION_MIN = 0.75

# Spread Detection
# Required headroom above the fee-adjusted break-even, per share.
SLIPPAGE_BUFFER = 0.01
MAX_FILLABLE_CAP = 1000          # hard upper bound on the max_fillable_size search
# Staleness gate: the two legs come from independent feeds that update at
# different rates. If either leg's snapshot is older than this, the spread is
# a comparison against a stale quote, not an arb — so it is discarded.
MAX_LEG_STALENESS_SECONDS = 2.0

# Runtime
RECONNECT_BASE_SECONDS = 1.0     # WS reconnect backoff start (doubles per failure)
RECONNECT_MAX_SECONDS = 30.0     # WS reconnect backoff cap

# Longest silence tolerated on an open socket before treating it as dead.
#
# A venue can stop sending while the connection stays perfectly healthy — no
# close frame, no error, pings still answered — and an unbounded read waits on
# it forever. The 30-day run lost 15.1h, 12.6h and 11.9h that way, ingesting
# nothing while the process looked fine. No transport-level keepalive detects
# this; only the absence of DATA does.
#
# The tradeoff is resnapshot cost: a reconnect makes Kalshi resend ~3,000 book
# snapshots, so too low a value trades a rare stall for constant churn. Three
# minutes is far below the observed stalls (hours) and far above any plausible
# quiet period at ~930 updates/sec.
STREAM_IDLE_TIMEOUT_SECONDS = 180.0

# Output Paths
LOG_PATH = "opportunities.jsonl"              # append-only event log (backtest)
LATEST_LOG_PATH = "opportunities_latest.jsonl"  # keyed snapshot, one line per open pair
MATCH_LOG_PATH = "matches.json"
# Throttle on EDGE_CHANGE samples for live game markets, per pair. Applies only
# to the non-viable case: viable evaluations are never throttled, because a
# window opens and closes ON a book update and throttling it caps duration
# resolution at the timer. 86% of Phase 1 windows fell below this and read as 0s.
EDGE_CHANGE_THROTTLE_SECONDS = 30

# How often a quiet, non-viable pair is recorded anyway. This is the honest
# DENOMINATOR: without it, "monitored continuously and never viable" and "never
# monitored" are the same absence of rows, and every rate computed over the
# match set is a floor rather than a measurement. Not about catching arbitrage
# — about proving where there wasn't any. At ~1,300 tracked pairs this is
# ~375k rows/day, which is most of the write volume, deliberately.
HEARTBEAT_SECONDS = 300

# Fees
# Kalshi taker fee = KALSHI_FEE_COEFFICIENT * price * (1 - price) per contract.
KALSHI_FEE_COEFFICIENT = 0.07

POLY_US_TAKER_THETA = 0.05      # taker pays:   0.05 * p * (1 - p) per contract
POLY_US_MAKER_THETA = -0.0125   # maker rebate (negative = credited back); Phase 2

# --- daily match-set refresh ----------------------------------------------- #
# Hour (UTC) at which the collector re-discovers both catalogs, adds
# newly-listed markets and drops settled ones, in process.
#
# 78% of tracked pairs are tied to a single game (props 3,042 + lines 2,134 +
# structured 626 of 7,393), so the match set decays DAILY: discovery ran once at
# startup and `books` sat at 14,780 for 14 days, which means settled games were
# still subscribed — the mechanism behind the stale-book artifacts (NASCAR at
# 83c, F1 constructors at 81c on post-event books).
#
# 11:00 UTC is 07:00 US Eastern: after overnight settlement has swept the
# previous slate, before the day's slate lists, and the quiet hour with the
# fewest live windows for a resubscribe to disturb.
MATCH_REFRESH_HOUR_UTC = 11

# How long a venue gets to acknowledge a live subscription mutation before the
# refresh gives up on it and reconnects that connection with the corrected list.
# A refresh that believes it succeeded while the venue ignored it is the same
# failure class as the subscription-cap bug that ran healthy-looking for two
# hours, so every mutation is verified and an unverified one is never assumed.
SUBSCRIPTION_ACK_TIMEOUT_SECONDS = 10.0

# Polymarket acks an `unsubscribe` but says NOTHING on a successful subscribe —
# only a later `error` frame reveals a rejection (that is how 75% of a
# subscription set was once lost silently). So after a resync's subscribes, the
# shard is watched for this long and any new error frame condemns the mutation.
SUBSCRIBE_ERROR_GRACE_SECONDS = 3.0
