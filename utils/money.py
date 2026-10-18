"""Money helpers. Money is always ``int`` cents; floats never enter money math."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def _check(cents: int) -> int:
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError(f"money must be int cents, got {type(cents).__name__}: {cents!r}")
    return cents


def fmt_cents(cents: int) -> str:
    """``1234`` -> ``"$12.34"``; ``-150`` -> ``"-$1.50"``; ``123456`` -> ``"$1,234.56"``."""
    _check(cents)
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rem:02d}"


def parse_dollars(value: str | int | float | Decimal) -> int:
    """``"$12.34"`` / ``"12.34"`` / ``"1,234"`` / ``12.5`` -> cents (half-up rounding)."""
    if isinstance(value, bool):
        raise TypeError("bool is not money")
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("$", "").replace("USD", "").strip()
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()").strip()
        try:
            d = Decimal(s)
        except InvalidOperation as e:
            raise ValueError(f"not a dollar amount: {value!r}") from e
        if neg:
            d = -d
    elif isinstance(value, float):
        d = Decimal(repr(value))
    else:
        d = Decimal(value)
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_dollars_str(cents: int) -> str:
    """``1234`` -> ``"12.34"`` (no symbol; for APIs/CSV)."""
    _check(cents)
    sign = "-" if cents < 0 else ""
    d, r = divmod(abs(cents), 100)
    return f"{sign}{d}.{r:02d}"


def sum_cents(values: Iterable[int]) -> int:
    total = 0
    for v in values:
        total += _check(v)
    return total


def line_total(unit_cents: int, qty: int) -> int:
    _check(unit_cents)
    if isinstance(qty, bool) or not isinstance(qty, int):
        raise TypeError("qty must be int")
    return unit_cents * qty


def apply_pct_off(cents: int, pct: int | float) -> int:
    """Price after ``pct`` percent off, rounded half-up to the cent. ``apply_pct_off(1000, 25) == 750``."""
    _check(cents)
    if not 0 <= pct <= 100:
        raise ValueError(f"pct must be in [0, 100], got {pct}")
    off = (Decimal(cents) * Decimal(str(pct)) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return cents - int(off)


def pct_off(original_cents: int, sale_cents: int) -> int:
    """Whole-percent discount from ``original`` to ``sale`` (half-up). ``pct_off(1000, 750) == 25``."""
    _check(original_cents)
    _check(sale_cents)
    if original_cents <= 0:
        return 0
    d = (Decimal(original_cents - sale_cents) * 100 / Decimal(original_cents)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return int(d)


def savings(original_cents: int, sale_cents: int) -> int:
    return max(0, _check(original_cents) - _check(sale_cents))
