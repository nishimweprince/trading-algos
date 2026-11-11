"""Reserve math: perp USD reserve (beta_P) and inverse-future SOL margin fraction (phi)."""

from __future__ import annotations


def beta_p(q_runup: float, mm: float) -> float:
    """USD reserve per $ of long notional for one margin call over the exit window."""
    return (1.0 + mm) * q_runup


def k_p(im: float, beta: float) -> float:
    """Capital multiplier for the perp variant: long + initial margin + reserve."""
    return 1.0 + im + beta


def phi(U: float, mm: float) -> float:
    """Minimum SOL margin, as a fraction of contract SOL size C = N/F0, after a rally U.

    For an inverse short of USD size N with SOL margin m, equity in SOL after the
    price moves to F0(1+U) is m - C*U/(1+U); maintenance needs mm*C/(1+U). So
    m/C >= (U + mm)/(1 + U). phi(0) = mm, phi is increasing, phi < 1 for mm < 1,
    and phi -> 1 as U -> inf: a short fully collateralized in SOL cannot be liquidated.
    """
    if U < 0:
        U = 0.0
    return (U + mm) / (1.0 + U)


def margin_fraction(im: float, U: float, mm: float) -> float:
    """SOL posted as margin, as a fraction of the long: max(im, phi(U))."""
    return max(im, phi(U, mm))
