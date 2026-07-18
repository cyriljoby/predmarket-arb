"""Central configuration: fee constants, thresholds, paths.

Single source of truth for tunable values so no magic numbers hide in logic.
"""

# Market Matching
MATCH_THRESHOLD = 0.75            # min string similarity to flag a market pair. Needs to be tuned manually
RESOLUTION_DATE_TOLERANCE_DAYS = 10  # resolution dates must fall within this window
# Futures/outright matching (structured). Venues pad outright resolution dates
# more loosely than games, so a wider window; season disambiguation still leans
# on it because competition strings drop the year.
FUTURES_DATE_TOLERANCE_DAYS = 30
FUTURES_ENTITY_MIN = 0.6         # min entity-name score (reuses competitor_score)
# Min normalized-competition overlap. 0.65 (not 0.5) because a single shared
# common token — "James" in "James Harden" vs "LeBron James", "NASCAR"/"Series"
# across Truck vs Cup — otherwise pairs different contracts. Same-competition
# matches score ~0.75-1.0 and clear it comfortably.
FUTURES_COMPETITION_MIN = 0.65

# Spread Detection
SLIPPAGE_BUFFER = 0.01            # required headroom above fee-adjusted break-even (per share)
MAX_FILLABLE_CAP = 1000          # hard upper bound on the max_fillable_size search
# Staleness gate: the two legs come from independent feeds that updated at different rates. If either leg is too old, we dont trust the srpead
MAX_LEG_STALENESS_SECONDS = 2.0

# Runtime
POLL_INTERVAL_SECONDS = 30       # fallback polling interval if a WebSocket drops
RECONNECT_BASE_SECONDS = 1.0     # WS reconnect backoff start (doubles per failure)
RECONNECT_MAX_SECONDS = 30.0     # WS reconnect backoff cap

# Output Paths
LOG_PATH = "opportunities.jsonl"              # append-only event log (backtest)
LATEST_LOG_PATH = "opportunities_latest.jsonl"  # keyed snapshot, one line per open pair
MATCH_LOG_PATH = "matches.json"
LOG_HEARTBEAT_SECONDS = 30    # append a per-pair sample at most this often (bounds log size)

# Fees
# Kalshi taker fee = KALSHI_FEE_COEFFICIENT * price * (1 - price) per contract.
KALSHI_FEE_COEFFICIENT = 0.07

POLY_US_TAKER_THETA = 0.05      # taker pays:   0.05 * p * (1 - p) per contract
POLY_US_MAKER_THETA = -0.0125   # maker rebate (negative = credited back); Phase 2