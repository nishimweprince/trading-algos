"""Fixed-quantity and equity-based fixed-fractional sizing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SizingDecision:
    qty: float
    denominator_pips: float
    pair_risk_cash: float | None
    pair_risk_pct: float | None


def risk_denominator_pips(s_pips: float, slippage_pips_per_side: float) -> float:
    """One-R stop distance plus adverse slippage on entry and stop exit."""
    if s_pips <= 0:
        raise ValueError("S in pips must be positive")
    if slippage_pips_per_side < 0:
        raise ValueError("slippage must be non-negative")
    return s_pips + 2 * slippage_pips_per_side


def fixed_fractional_size(
    *,
    equity_cash: float,
    s_pips: float,
    slippage_pips_per_side: float,
    dollars_per_pip_per_qty: float,
    risk_pct_per_r: float,
    max_pair_risk_pct: float,
) -> SizingDecision:
    if equity_cash <= 0:
        raise ValueError("equity must be positive for fixed-fractional sizing")
    if dollars_per_pip_per_qty <= 0:
        raise ValueError("dollars per pip per quantity must be positive")
    denominator = risk_denominator_pips(s_pips, slippage_pips_per_side)
    per_r_cash = equity_cash * risk_pct_per_r / 100.0
    qty_for_one_r = per_r_cash / (denominator * dollars_per_pip_per_qty)
    pair_cash_cap = equity_cash * max_pair_risk_pct / 100.0
    qty_for_pair_cap = pair_cash_cap / (2 * denominator * dollars_per_pip_per_qty)
    qty = min(qty_for_one_r, qty_for_pair_cap)
    pair_risk_cash = 2 * denominator * dollars_per_pip_per_qty * qty
    return SizingDecision(
        qty=qty,
        denominator_pips=denominator,
        pair_risk_cash=pair_risk_cash,
        pair_risk_pct=100.0 * pair_risk_cash / equity_cash,
    )


def fixed_qty_size(
    *,
    qty: float,
    equity_cash: float | None,
    s_pips: float,
    slippage_pips_per_side: float,
    dollars_per_pip_per_qty: float | None,
) -> SizingDecision:
    denominator = risk_denominator_pips(s_pips, slippage_pips_per_side)
    if equity_cash is None or dollars_per_pip_per_qty is None:
        return SizingDecision(
            qty=qty,
            denominator_pips=denominator,
            pair_risk_cash=None,
            pair_risk_pct=None,
        )
    risk_cash = 2 * denominator * dollars_per_pip_per_qty * qty
    return SizingDecision(
        qty=qty,
        denominator_pips=denominator,
        pair_risk_cash=risk_cash,
        pair_risk_pct=100.0 * risk_cash / equity_cash,
    )
