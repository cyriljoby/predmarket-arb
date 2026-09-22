"""The MarketDataFeed interface — the venue plug-in seam.

Every venue adapter satisfies this Protocol. All platform-specific wire format,
auth, and reconnect logic stays quarantined behind it, so the matcher and
detector never know which venue produced a Market. Adding a venue = adding one
module that conforms to this — touching nothing downstream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from pmarb.models import Market


class SubscriptionMutationError(RuntimeError):
    """A venue did not confirm a live subscription change.

    Raised rather than logged-and-ignored because the two look identical from
    inside the process and only one of them is survivable: a refused
    subscription arrives as silence, which is indistinguishable from an idle
    market — that is exactly how a 75% subscription loss once ran
    healthy-looking for two hours. Every caller answers this by reconnecting the
    affected connection with the corrected list, never by assuming.
    """


@runtime_checkable
class MarketDataFeed(Protocol):
    platform: str

    async def fetch_markets(self) -> list[Market]:
        """Discover active markets as metadata snapshots (depth may be empty).

        Used once at startup by the matcher, which only needs question text,
        resolution date, and category — not order-book depth.
        """
        ...

    def stream_books(self) -> AsyncIterator[Market]:
        """Yield a fresh, full-depth Market snapshot on every order-book update.

        An async generator: `async for market in feed.stream_books(): ...`.
        This is what drives the detector in real time.
        """
        ...

    async def resync(self, markets: list[Market]) -> dict:
        """Change the LIVE subscription of a running `stream_books` to
        `markets`, without restarting the stream.

        The daily match refresh needs this because a restart destroys
        `last_append`, and `was_viable` there is the only thing that pins an
        open window's close. Implementations MUST verify the venue applied the
        change and fall back to reconnecting the affected connection with the
        corrected list when it did not.
        """
        ...
