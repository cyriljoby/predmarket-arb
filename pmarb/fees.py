"""Per-venue fee functions, all in **per-share** (per-contract) units.

Both venues we trade — Kalshi and Polymarket US — charge the identical quadratic
shape, `coefficient * price * (1 - price)`, differing only in the coefficient.
So there is really one fee function; the rest are thin named wrappers the
detector calls without needing to know the venue. Adding a venue later means
adding one constant + one wrapper — the fee seam.
"""

from __future__ import annotations

from pmarb import config


def quadratic_fee(coefficient: float, price: float) -> float:
    """Per-share fee: ``coefficient * price * (1 - price)``.

    Concave in price (peaks at 0.50). Two rules the detector must honor:
      * compute on the actual depth-walked FILL price, not top-of-book;
      * accumulate PER depth level, never on an average price — because the
        function is concave, ``fee(avg_price)`` overstates the true fee.

    A negative coefficient (e.g. a maker rebate) yields a negative fee — a
    credit — so the same formula serves makers and takers alike.
    """
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price out of range [0, 1]: {price}")
    return round(coefficient * price * (1.0 - price), 4)


def kalshi_fee_per_share(price: float) -> float:
    """Kalshi taker fee per contract. Peaks at ~$0.0175 at $0.50."""
    return quadratic_fee(config.KALSHI_FEE_COEFFICIENT, price)


def poly_us_taker_fee_per_share(price: float) -> float:
    """Polymarket US taker fee per contract. Peaks at $0.0125 at $0.50."""
    return quadratic_fee(config.POLY_US_TAKER_THETA, price)


def poly_us_maker_rebate_per_share(price: float) -> float:
    """Polymarket US maker rebate per contract (negative = credit).

    Phase 2 only — Phase 1 detection conservatively assumes taker fills on both
    legs, so it never relies on the rebate.
    """
    return quadratic_fee(config.POLY_US_MAKER_THETA, price)
