from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from solcarry.analytics.reserve import beta_p, k_p, margin_fraction, phi


def test_beta_and_k():
    assert beta_p(0.10, 0.05) == pytest.approx(0.105)
    assert k_p(0.10, 0.105) == pytest.approx(1.205)


@given(st.floats(0, 0.99))
def test_phi_zero(mm):
    assert phi(0.0, mm) == pytest.approx(mm)


@given(st.floats(0, 0.99), st.floats(0, 100), st.floats(1e-6, 100))
def test_phi_monotonic_and_below_one(mm, u, du):
    assert phi(u + du, mm) >= phi(u, mm) - 1e-12
    assert phi(u, mm) < 1.0


@given(st.floats(0, 0.5))
def test_phi_limit(mm):
    assert phi(1e9, mm) == pytest.approx(1.0, abs=1e-6)


@given(st.floats(0, 0.5), st.floats(0, 100))
def test_full_collateral_never_liquidates(mm, U):
    """m = C (fully collateralized in SOL) satisfies m/C >= phi(U) for rallies up to 100x."""
    assert 1.0 >= phi(U, mm)


def test_margin_fraction_floor_at_im():
    assert margin_fraction(0.10, 0.0, 0.05) == 0.10
    assert margin_fraction(0.10, 1.0, 0.05) == pytest.approx(0.525)
