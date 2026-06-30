"""The MarketDataFeed interface — the venue plug-in seam.

Every venue adapter satisfies this Protocol. All platform-specific wire format,
auth, and reconnect logic stays quarantined behind it, so the matcher and
detector never know which venue produced a Market. Adding a venue = adding one
module that conforms to this — touching nothing downstream.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from pmarb.models import Market


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
