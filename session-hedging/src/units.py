"""Performance units: pips_raw, pips_weighted, R, cash.

Averaging raw pips across variable stop distance S disagrees in sign with averaging R.
``pips_weighted`` is the primary additive series; ``r_multiple`` is the sign-of-truth when S varies.
"""

from __future__ import annotations


def pips_raw(*, exit_px: float, entry: float, pip_size: float, is_long: bool) -> float:
    delta = (exit_px - entry) if is_long else (entry - exit_px)
    return delta / pip_size


def pips_weighted(raw: float, *, qty: float, qty_ref: float) -> float:
    if qty_ref == 0:
        raise ValueError("QTY_REF must be non-zero")
    return raw * (qty / qty_ref)


def r_multiple(raw: float, *, s_pips: float) -> float:
    if s_pips == 0:
        raise ValueError("S in pips must be non-zero")
    return raw / s_pips


def cash(
    weighted: float, *, dollars_per_pip_per_qty: float | None, qty_ref: float
) -> float | None:
    """Convert an additive weighted-pip amount into account cash."""
    if dollars_per_pip_per_qty is None:
        return None
    return weighted * dollars_per_pip_per_qty * qty_ref


def conversion_factor(
    *, unit: str, dollars_per_pip_per_qty: float | None, qty_ref: float
) -> float:
    """Multiplier from an additive weighted-pip amount into the reporting unit.

    Pips report themselves. Dollars scale by the cash value of one pip at ``QTY_REF``.
    R is deliberately absent: it is a ratio and is never converted.
    """
    if unit != "dollars":
        return 1.0
    if dollars_per_pip_per_qty is None:
        raise ValueError("dollar reporting requires a dollars-per-pip rate")
    return dollars_per_pip_per_qty * qty_ref
