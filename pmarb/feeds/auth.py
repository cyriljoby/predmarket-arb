"""Request signing for the venues' authenticated endpoints.

Both venues sign the same message — ``timestamp + method + path`` — but with
different algorithms and header names:

  * Kalshi:        RSA-PSS / SHA256 over the account's RSA private key
  * Polymarket US: Ed25519 over the base64 secret (first 32 bytes = seed)

Each function returns the auth headers to attach to a request. Signing is
identical whether the request reads or trades; Phase 1 only ever signs GETs.
The `path` must include the API version prefix and exclude any query string.
"""

from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pmarb.credentials import KalshiCredentials, PolymarketUSCredentials


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def kalshi_headers(creds: KalshiCredentials, method: str, path: str) -> dict[str, str]:
    """Auth headers for a Kalshi request.

    `path` includes the `/trade-api/v2` prefix and excludes the query string
    (e.g. sign `/trade-api/v2/portfolio/balance`, not `...?limit=5`).
    """
    timestamp = _now_ms()
    message = f"{timestamp}{method}{path}".encode()
    private_key = serialization.load_pem_private_key(
        creds.private_key_pem.encode(), password=None
    )
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": creds.key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


def polymarket_us_headers(
    creds: PolymarketUSCredentials, method: str, path: str
) -> dict[str, str]:
    """Auth headers for a Polymarket US request.

    The secret is a base64 Ed25519 key; the first 32 bytes are the seed.
    `path` includes the `/v1` prefix and excludes the query string. The
    timestamp must be within 30 seconds of server time.
    """
    timestamp = _now_ms()
    message = f"{timestamp}{method}{path}".encode()
    seed = base64.b64decode(creds.secret_key)[:32]
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    signature = private_key.sign(message)
    return {
        "X-PM-Access-Key": creds.key_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": base64.b64encode(signature).decode(),
    }
