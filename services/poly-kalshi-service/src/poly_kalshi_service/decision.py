"""The kpc rule (manual §3), pure: the live engine and the research replay share it.

    gap = kalshi_yes_mid - poly_yes_mid
    |gap| >= threshold -> buy the cheap Polymarket leg as a taker, hold to settlement

Three semantics are load-bearing and must not be "improved" here:

1. The first crossing decides: no mean, no persistence, no confirmation
   (every such variant was tested and lost; manual §9).
2. A crossing whose entry price is above the cap returns ``SKIP``, and the
   caller consumes the window: no retry at a later, cheaper tick.
3. Nothing here exits. Positions are held to settlement.
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
) -> Decision:
    if already_traded:
        return _none("traded")
    if not kalshi_yes.valid or not poly_yes.valid:
        return _none("invalid_leg")
    if sec_into < params.entry_early_s:
        return _none("too_early")
    if sec_into > WINDOW_S - params.entry_late_s:
        return _none("too_late")

    k_mid, p_mid = kalshi_yes.mid, poly_yes.mid
    if k_mid is None or p_mid is None:
        return _none("invalid_leg")
    gap = k_mid - p_mid
    if abs(gap) < params.gap_threshold:
        return _none("below_threshold", gap)

    buy_yes = gap > 0
    if not buy_yes and not poly_no.valid:
        return _none("invalid_leg", gap)  # buying NO needs the NO book
    entry_px = poly_yes.ask if buy_yes else poly_no.ask
    if entry_px is None or entry_px <= 0 or entry_px >= 1:
        return _none("bad_price", gap)
    if entry_px > params.max_entry_px:
        return Decision(Action.SKIP, "above_cap", gap, buy_yes, entry_px)

    contracts = math.floor(params.notional_usd / entry_px)
    if contracts < 1:
        return _none("too_small", gap)
    return Decision(Action.ENTER, "", gap, buy_yes, entry_px, contracts)
