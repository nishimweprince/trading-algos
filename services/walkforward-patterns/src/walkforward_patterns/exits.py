"""Exit rules shared by replay and live.

`TimeExit` decides the time stop and the Friday flat exactly like `hs.sim` (swing mode): the position is closed at
the start of M1 bar k when k - j >= 2880 traded minutes, when bar k is at/after 16:45 on the Friday trading day, or
when bar k is the first bar of a new trading day after a Friday trading day.

`SimPosition` adds the research fill rules for replay (SL before TP in the same minute; a minute opening beyond the
SL fills at that open). Live, SL/TP sit at the broker and only the time exits are ours.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import FRIDAY_FLAT_TOD, tod, trading_day, weekday

MAX_HOLD = 2880


@dataclass
class TimeExit:
    j: int  # M1 index of the entry minute
    max_hold: int = MAX_HOLD

    def due(self, k: int, t: int, prev_t: int | None) -> str | None:
        if k - self.j >= self.max_hold:
            return "TIME"
        d = trading_day(t)
        if weekday(d) == 4 and tod(t) >= FRIDAY_FLAT_TOD:
            return "FRIDAY"
        if k > self.j and prev_t is not None:
            pd_ = trading_day(prev_t)
            if d != pd_ and weekday(pd_) == 4:
                return "FRIDAY"
        return None


@dataclass
class SimPosition:
    sid: str
    side: int
    j: int
    t_entry: int
    e: float
    sl: float
    tp: float
    risk: float
    cost: float
    signal_bar: int
    timer: TimeExit = None  # type: ignore[assignment]
    exit_px: float | None = None
    exit_t: int | None = None
    exit_k: int | None = None
    reason: str | None = None
    last_c: float | None = None

    def __post_init__(self):
        self.timer = TimeExit(self.j)

    def on_m1(self, k: int, t: int, prev_t: int | None, o: float, h: float, l: float, c: float) -> bool:
        """Process M1 bar k (k >= j). Returns True when the position closed on this bar."""
        why = self.timer.due(k, t, prev_t)
        if why is not None:
            self.exit_px = self.last_c if k > self.j else self.e
            self.reason = "TIME/FRIDAY"
            self.exit_k, self.exit_t = k, t
            return True
        s = self.side
        if s == 1:
            if o <= self.sl:
                px, r = o, "SL"
            elif l <= self.sl:
                px, r = self.sl, "SL"
            elif h >= self.tp:
                px, r = self.tp, "TP"
            else:
                px = None
        else:
            if o >= self.sl:
                px, r = o, "SL"
            elif h >= self.sl:
                px, r = self.sl, "SL"
            elif l <= self.tp:
                px, r = self.tp, "TP"
            else:
                px = None
        if px is not None:
            self.exit_px, self.reason, self.exit_k, self.exit_t = px, r, k, t
            return True
        self.last_c = c
        return False

    @property
    def R(self) -> float:
        return (self.side * (self.exit_px - self.e) - self.cost) / self.risk
