"""API credentials, loaded from the environment — never hardcoded, never committed.

Reads an optional `.env` file at the repo root (so you don't have to export vars
by hand), then falls back to the real environment. See `.env.example` for the
required variables.

Credentials are needed ONLY for the live WebSocket feeds. Public REST endpoints
(market discovery, order-book snapshots) need none, so importing this module has
no effect until you actually call a `from_env()` constructor — keeping tests and
REST-only code credential-free.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency). Does not overwrite vars already set
    in the real environment, which always wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(_REPO_ROOT / ".env")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"missing required environment variable {name!r}; "
            f"copy .env.example to .env and fill it in"
        )
    return value


@dataclass(frozen=True)
class KalshiCredentials:
    """Kalshi API key pair. The private key signs every request (RSA-PSS)."""

    key_id: str
    private_key_pem: str

    @classmethod
    def from_env(cls) -> "KalshiCredentials":
        key_path = Path(_require("KALSHI_PRIVATE_KEY_PATH")).expanduser()
        return cls(
            key_id=_require("KALSHI_API_KEY_ID"),
            private_key_pem=key_path.read_text(),
        )


@dataclass(frozen=True)
class PolymarketUSCredentials:
    """Polymarket US API key pair. The secret key signs requests (Ed25519)."""

    key_id: str
    secret_key: str

    @classmethod
    def from_env(cls) -> "PolymarketUSCredentials":
        return cls(
            key_id=_require("POLYMARKET_US_KEY_ID"),
            secret_key=_require("POLYMARKET_US_SECRET_KEY"),
        )
