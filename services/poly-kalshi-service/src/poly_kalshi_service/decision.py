"""The kpc rule (manual §3), pure: the live engine and the research replay share it.

    gap = kalshi_yes_mid - poly_yes_mid
    |gap| >= threshold -> buy the cheap Polymarket leg as a taker, hold to settlement

Three semantics are load-bearing and must not be "improved" here:

1. The first crossing decides: no mean, no persistence, no confirmation
   (every such variant was tested and lost; manual §9).
2. A crossing whose entry price is above the cap returns ``SKIP``, and the
   caller consumes the window: no retry at a later, cheaper tick.
3. Nothing here exits. Positions are held to settlement.

``max_kalshi_spread`` and ``max_poly_spread`` (default off) are filters for
research variants: a crossing measured on a Kalshi (Polymarket YES) book wider
than this returns ``NONE`` with reason ``kalshi_spread`` (``poly_spread``) and
does **not** consume the window, since a gap on a wide mid is not yet a signal.
On the Helix discovery days, crossings with a Polymarket spread over 0.10 lost
2.3¢/share and those at 1¢ made 9.1¢ (research/RESULTS.md). With both unset the
rule is exactly the above.

Three more research filters (default off; PLAN after epoch 4, PREREG part 3):

- ``min_entry_px``: a crossing whose entry price is below the floor returns
  ``SKIP`` (reason ``below_floor``) and consumes the window, like the cap.
- ``kalshi_extreme``: when true, a crossing where Kalshi's mid is no further
  from 0.5 than Polymarket's returns ``SKIP`` (reason ``kalshi_lags``) and
  consumes the window. Such a gap is Kalshi trailing a move Polymarket already
  priced, not Kalshi leading. Epoch 3: 77% wins when Kalshi was the more extreme
  venue (n=26) and 38% when it was not (n=87); in epoch 4, 15 of 16 entries were
  of the second kind and all lost.
- ``skip_hours_utc``: no decision (``NONE``, reason ``hours``) while the UTC hour,
  passed as ``utc_hour``, is in the set. Windows are aligned to the quarter
  hour, so a window never spans two hours. It applies only when the caller
  passes ``utc_hour``.

One more (default off; PREREG part 4):

- ``max_poly_lag_ms``: no decision (``NONE``, reason ``poly_lag``, the window is
  not consumed) while the cheap Polymarket leg's last update arrived more than
  this many ms after the venue stamped it (``poly_lag_ms``, passed by the
  caller). A late frame means our view of that book is old by at least that
  much: the ask we would price off may already be gone. It is the transport
  lag of the book's latest frame, not the time since the book last changed,
  because a quiet Polymarket book while Kalshi moves is the signal itself.
  It applies only when the caller passes ``poly_lag_ms``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .book import Quote

__all__ = ["WINDOW_S", "Action", "Decision", "StrategyParams", "decide"]

WINDOW_S = 900


@dataclass(frozen=True, slots=True)
class StrategyParams:
    gap_threshold: float = 0.20
    max_entry_px: float = 0.60
    notional_usd: float = 50.0
    entry_early_s: int = 20
    entry_late_s: int = 60
    max_kalshi_spread: float | None = None
    max_poly_spread: float | None = None
    min_entry_px: float | None = None
    kalshi_extreme: bool | None = None
    skip_hours_utc: tuple[int, ...] | None = None
    max_poly_lag_ms: float | None = None


class Action(StrEnum):
    NONE = "none"
    SKIP = "skip"  # crossed, but above the price cap: the window is consumed
    ENTER = "enter"


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str = ""
    gap: float | None = None
    buy_yes: bool | None = None
    entry_px: float | None = None
    contracts: int = 0

    @property
    def consumes_window(self) -> bool:
        return self.action is not Action.NONE


def _none(reason: str, gap: float | None = None) -> Decision:
    return Decision(Action.NONE, reason, gap)


def decide(
    kalshi_yes: Quote,
    poly_yes: Quote,
    poly_no: Quote,
    params: StrategyParams,
    sec_into: float,
    already_traded: bool,
    utc_hour: int | None = None,
    poly_lag_ms: tuple[float | None, float | None] | None = None,
) -> Decision:
    """``poly_lag_ms``: the transport lag (ms) of the latest frame on the YES and
    the NO book, read only when ``params.max_poly_lag_ms`` is set."""
    if already_traded:
        return _none("traded")
    if params.skip_hours_utc and utc_hour is not None and utc_hour in params.skip_hours_utc:
        return _none("hours")
    # Timing before book validity. It changes no decision, only the reason: near
    # expiry the books go one-sided because the outcome is all but decided, and
    # that is "too late", not a data fault. ``invalid_leg`` then counts book
    # problems inside the entry window only.
    if sec_into < params.entry_early_s:
        return _none("too_early")
    if sec_into > WINDOW_S - params.entry_late_s:
        return _none("too_late")
    if not kalshi_yes.valid or not poly_yes.valid:
        return _none("invalid_leg")

    k_mid, p_mid = kalshi_yes.mid, poly_yes.mid
    if k_mid is None or p_mid is None:
        return _none("invalid_leg")
    gap = k_mid - p_mid
    if abs(gap) < params.gap_threshold:
        return _none("below_threshold", gap)
    if params.max_kalshi_spread is not None:
        # A valid quote has both sides (its mid exists), so the ``or`` never applies.
        spread = (kalshi_yes.ask or 0.0) - (kalshi_yes.bid or 0.0)
        if spread > params.max_kalshi_spread + 1e-9:
            return _none("kalshi_spread", gap)
    if params.max_poly_spread is not None:
        spread = (poly_yes.ask or 0.0) - (poly_yes.bid or 0.0)
        if spread > params.max_poly_spread + 1e-9:
            return _none("poly_spread", gap)

    buy_yes = gap > 0
    if not buy_yes and not poly_no.valid:
        return _none("invalid_leg", gap)  # buying NO needs the NO book
    if params.max_poly_lag_ms is not None and poly_lag_ms is not None:
        lag = poly_lag_ms[0] if buy_yes else poly_lag_ms[1]
        if lag is not None and lag > params.max_poly_lag_ms:
            return _none("poly_lag", gap)
    entry_px = poly_yes.ask if buy_yes else poly_no.ask
    if entry_px is None or entry_px <= 0 or entry_px >= 1:
        return _none("bad_price", gap)
    if entry_px > params.max_entry_px:
        return Decision(Action.SKIP, "above_cap", gap, buy_yes, entry_px)
    if params.min_entry_px is not None and entry_px < params.min_entry_px - 1e-9:
        return Decision(Action.SKIP, "below_floor", gap, buy_yes, entry_px)
    if params.kalshi_extreme and abs(k_mid - 0.5) <= abs(p_mid - 0.5):
        return Decision(Action.SKIP, "kalshi_lags", gap, buy_yes, entry_px)

    contracts = math.floor(params.notional_usd / entry_px)
    if contracts < 1:
        return _none("too_small", gap)
    return Decision(Action.ENTER, "", gap, buy_yes, entry_px, contracts)
