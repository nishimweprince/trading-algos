"""Retracement of the latest impulse A -> B.

``retr = (B - P) / (B - A)``: 0 at B, 1 at A, the same formula for longs
(A low, B high) and shorts (A high, B low).

At retest time the post-breakout swing B is usually not confirmed yet (a 1H
swing needs R more bars), so the retest FSM measures B as the running extreme
since the breakout and A as the last confirmed opposite 1H swing before it.
Both are known at the bar's close, so nothing leaks from the future.
"""

from __future__ import annotations

__all__ = ["in_band", "retracement"]


def retracement(a: float, b: float, p: float) -> float:
    if b == a:
        return float("nan")
    return (b - p) / (b - a)


def in_band(retr: float, band: tuple[float, float]) -> bool:
    return band[0] <= retr <= band[1]
