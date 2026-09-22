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


def _kalshi_private_key_pem() -> str:
    """The Kalshi RSA private key, inline from the environment or read from disk.

    Two sources because the two places this runs disagree about what a secret
    is. On a laptop the key is a file you downloaded once and never moved, so a
    path is the natural handle. In a container there is no such file and no
    volume worth mounting for one value — the secret arrives as an environment
    variable, which is the only thing an env-only secret store can feed.

    KALSHI_PRIVATE_KEY wins when both are set: if someone went to the trouble
    of injecting the key itself, a stale path inherited from a copied .env must
    not silently shadow it.
    """
    inline = os.environ.get("KALSHI_PRIVATE_KEY")
    if inline:
        # A PEM shoved through an env var usually arrives with its newlines
        # escaped — `docker run -e`, compose interpolation and most secret
        # stores all hand you the two characters backslash-n rather than a real
        # line break. cryptography rejects that as a malformed key with an
        # error that says nothing about newlines, so normalize here rather than
        # debug it at the signing call.
        return inline.replace("\\n", "\n")
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if path:
        return Path(path).expanduser().read_text()
    raise RuntimeError(
        "missing Kalshi private key: set KALSHI_PRIVATE_KEY to the PEM itself "
        "(containers, secret stores) or KALSHI_PRIVATE_KEY_PATH to the file "
        "holding it (local runs); copy .env.example to .env and fill it in"
    )


@dataclass(frozen=True)
class KalshiCredentials:
    """Kalshi API key pair. The private key signs every request (RSA-PSS)."""

    key_id: str
    private_key_pem: str

    @classmethod
    def from_env(cls) -> KalshiCredentials:
        return cls(
            key_id=_require("KALSHI_API_KEY_ID"),
            private_key_pem=_kalshi_private_key_pem(),
        )


@dataclass(frozen=True)
class PolymarketUSCredentials:
    """Polymarket US API key pair. The secret key signs requests (Ed25519)."""

    key_id: str
    secret_key: str

    @classmethod
    def from_env(cls) -> PolymarketUSCredentials:
        return cls(
            key_id=_require("POLYMARKET_US_KEY_ID"),
            secret_key=_require("POLYMARKET_US_SECRET_KEY"),
        )
