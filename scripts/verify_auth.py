"""Live auth check: sign one read-only request per venue, confirm a 200.

Run: .venv/bin/python scripts/verify_auth.py

Prints ONLY the HTTP status and top-level field NAMES — never balances,
positions, or secret values. A 200 means the key authenticates for real.
"""

import json
import urllib.error
import urllib.request

from pmarb.credentials import KalshiCredentials, PolymarketUSCredentials
from pmarb.feeds.auth import kalshi_headers, polymarket_us_headers


# Polymarket US sits behind Cloudflare, which 1010-blocks non-browser User-Agents
# (the default urllib UA gets a 403 before auth is even checked). A browser-like
# UA is required on every request to that venue.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _get(url: str, headers: dict[str, str]):
    req = urllib.request.Request(
        url,
        headers={**headers, "Accept": "application/json", "User-Agent": _USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001 - surface any connection issue plainly
        return None, f"{type(e).__name__}: {e}"


def _check(name: str, url: str, headers: dict[str, str]) -> None:
    status, body = _get(url, headers)
    if status == 200:
        try:
            fields = list(json.loads(body).keys())
        except Exception:
            fields = "(non-JSON body)"
        print(f"{name}: ✅ 200 OK — authenticated. response fields: {fields}")
    else:
        print(f"{name}: ❌ status={status} — {str(body)[:200]}")


def main() -> None:
    kc = KalshiCredentials.from_env()
    k_path = "/trade-api/v2/portfolio/balance"
    _check(
        "Kalshi      ",
        f"https://api.elections.kalshi.com{k_path}",
        kalshi_headers(kc, "GET", k_path),
    )

    pc = PolymarketUSCredentials.from_env()
    p_path = "/v1/portfolio/positions"
    _check(
        "Polymarket US",
        f"https://api.polymarket.us{p_path}",
        polymarket_us_headers(pc, "GET", p_path),
    )


if __name__ == "__main__":
    main()
