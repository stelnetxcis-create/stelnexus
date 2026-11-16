"""Money handling.

Every amount in StelNexus is an **integer number of centavos** (PHP 1 = 100).
The original panel mixed float-ish PHP arithmetic with `SUM()` over decimal
columns; float money silently loses centavos and makes recompute non-idempotent.
Integer centavos + explicit rounding rules make settlement reproducible.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENTS = 100
MAX_CENTS = 10**13  # sanity ceiling, guards against overflow / fat-finger input


class MoneyError(ValueError):
    pass


def to_cents(value: str | int | float | Decimal, *, field: str = "amount") -> int:
    """Parse a user-supplied peso amount into centavos. Never uses binary floats."""
    if isinstance(value, bool):
        raise MoneyError(f"{field}: invalid amount")
    if isinstance(value, int):
        return value * CENTS
    try:
        dec = Decimal(str(value).replace(",", "").replace("₱", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"{field}: not a valid amount") from exc
    if not dec.is_finite():
        raise MoneyError(f"{field}: not a finite amount")
    if dec < 0:
        raise MoneyError(f"{field}: negative amounts are not allowed")
    cents = int((dec * CENTS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if cents > MAX_CENTS:
        raise MoneyError(f"{field}: amount exceeds the allowed maximum")
    return cents


def pesos(cents: int) -> Decimal:
    return (Decimal(int(cents)) / CENTS).quantize(Decimal("0.01"))


def fmt(cents: int | None, *, symbol: str = "₱") -> str:
    """Display helper used by templates: 123456 -> '₱1,234.56'."""
    if cents is None:
        cents = 0
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), CENTS)
    return f"{sign}{symbol}{whole:,}.{frac:02d}"


def bps_of(amount_cents: int, bps: int) -> int:
    """Floor(amount * bps / 10000). Basis points keep rates exact integers."""
    if bps < 0 or bps > 10000:
        raise MoneyError("basis points must be between 0 and 10000")
    return (int(amount_cents) * int(bps)) // 10000


def odds_of(amount_cents: int, odds_bps: int) -> int:
    """Floor(amount * multiplier) where the multiplier is in bps (19000 = 1.90x, max 100x)."""
    if odds_bps < 0 or odds_bps > 1_000_000:
        raise MoneyError("odds must be between 0 and 100.00x")
    return (int(amount_cents) * int(odds_bps)) // 10000


def payout_of(amount_cents: int, multiplier_x100: int) -> int:
    """Floor(amount * multiplier) for a **fixed-multiplier number game**.

    ``multiplier_x100``: 80000 = 800.00x. The bola (Tumbok-Sahod) game pays 800x, which is far
    above the 100.00x ceiling `odds_of` enforces for sabong, so it gets its own helper instead of
    widening that one (sabong maths must stay exactly as it is).

    Maximum 5,000.00x. Integer in, integer out: same inputs always give the same centavos.
    """
    if multiplier_x100 < 0 or multiplier_x100 > 500_000:
        raise MoneyError("multiplier must be between 0 and 5000.00x")
    return (int(amount_cents) * int(multiplier_x100)) // 100


def ratio(numerator: int, denominator: int, places: int = 2) -> Decimal:
    if denominator <= 0:
        return Decimal(0)
    q = Decimal(numerator) / Decimal(denominator)
    return q.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
