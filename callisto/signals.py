"""Turn classified posts into trade ideas: one idea per call, keeping only its EARLIEST post.

The channel posts a call and then many progress screenshots of the same position
("4120.39 -> 4125.17", "Still IN 140 pips"). Posts are folded into one idea when ticker and
direction match, the previous post of that idea is within GROUP_HOURS, and stated entries
(when both are known) are within ENTRY_MATCH_PIPS.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from config import CFG

CLOCK_OFFSETS_H = [8, 4, 3, 0, 1, 2, -5, -4, 5.5, 7, 9, 10]  # tz guesses for image clocks


@dataclass
class Idea:
    ticker: str
    direction: str
    first_post_id: int
    first_seen: str                  # ISO UTC time of the earliest post
    stated_entry: float | None       # entry known at first sight (price or range mid)
    entry_low: float | None = None
    entry_high: float | None = None
    stated_sl: float | None = None
    stated_tp: float | None = None
    pips_at_first_sight: float | None = None   # from the image/text, if shown
    image_clock_utc: str | None = None          # call time read from the image clock
    later_entry: float | None = None  # entry learned from a later post (reporting only)
    post_ids: list[int] = field(default_factory=list)
    last_seen: str = ""
    confidence: float = 0.0
    note: str = ""
    repost: bool = False   # first post shows a position whose exact entry was seen before

    @property
    def first_ts(self) -> datetime:
        return datetime.fromisoformat(self.first_seen)

    def to_dict(self) -> dict:
        return asdict(self)


def entry_ref(llm: dict) -> float | None:
    if llm.get("entry_price"):
        return float(llm["entry_price"])
    lo, hi = llm.get("entry_low"), llm.get("entry_high")
    if lo and hi:
        return (float(lo) + float(hi)) / 2
    return float(lo or hi) if (lo or hi) else None


def stated_pips(llm: dict, direction: str, ticker: str) -> float | None:
    if llm.get("pips_in_profit") is not None:
        return float(llm["pips_in_profit"])
    e, c = llm.get("entry_price"), llm.get("current_price")
    if e and c:
        sign = 1 if direction == "BUY" else -1
        return round(sign * (float(c) - float(e)) / CFG.pip(ticker), 1)
    return None


def clock_to_utc(clock: str | None, post_time: datetime) -> datetime | None:
    """Image clocks come from phones in different time zones. Pick the zone that puts the
    call at or shortly before the post (smallest non-negative gap, at most GROUP_HOURS)."""
    if not clock:
        return None
    try:
        hh, mm = (int(x) for x in clock.split(":"))
    except ValueError:
        return None
    best = None
    for off in CLOCK_OFFSETS_H:
        for day_shift in (0, -1):
            local_day = (post_time + timedelta(hours=off)).date() + timedelta(days=day_shift)
            t = (datetime(local_day.year, local_day.month, local_day.day, hh, mm,
                          tzinfo=UTC) - timedelta(hours=off))
            gap = (post_time - t).total_seconds()
            if 0 <= gap <= CFG.group_hours * 3600 and (best is None or gap < best[0]):
                best = (gap, t)
    return best[1] if best else None


def is_signal(llm: dict) -> bool:
    return bool(llm.get("kind") == "signal" and (llm.get("ticker") or CFG.default_ticker)
                and llm.get("direction") in ("BUY", "SELL")
                and float(llm.get("confidence") or 0) >= CFG.min_confidence)


class Grouper:
    """Stateful so the live monitor and the backtest share the same rule."""

    def __init__(self, seen_entries: set[str] | None = None):
        self.ideas: list[Idea] = []
        # Exact position open prices already shown by the channel. The channel recycles old
        # profit screenshots for marketing; a "new" idea showing one of these is a repost.
        self.seen_entries: set[str] = seen_entries if seen_entries is not None else set()

    def add(self, post: dict, llm: dict) -> tuple[Idea, bool] | None:
        """Returns (idea, is_new) for a signal post, else None."""
        if not is_signal(llm):
            return None
        ticker = (llm.get("ticker") or CFG.default_ticker).upper().split(".")[0].replace("/", "")
        if ticker in ("GOLD", "XAU"):
            ticker = "XAUUSD"
        direction = llm["direction"]
        t = datetime.fromisoformat(post["time"])
        e = entry_ref(llm)
        exact = f"{ticker}:{float(llm['entry_price']):.2f}" if llm.get("entry_price") else None
        repost = exact is not None and exact in self.seen_entries
        if exact:
            self.seen_entries.add(exact)
        tol = CFG.entry_match_pips * CFG.pip(ticker)
        for idea in reversed(self.ideas):
            if idea.ticker != ticker or idea.direction != direction:
                continue
            if t - datetime.fromisoformat(idea.last_seen) > timedelta(hours=CFG.group_hours):
                continue
            known = idea.stated_entry or idea.later_entry
            if e is not None and known is not None and abs(e - known) > tol:
                continue
            idea.post_ids.append(post["id"])
            idea.last_seen = post["time"]
            if e is not None and idea.stated_entry is None and idea.later_entry is None:
                idea.later_entry = e
            return idea, False
        clock = clock_to_utc(llm.get("image_clock"), t)
        idea = Idea(
            ticker=ticker, direction=direction, first_post_id=post["id"],
            first_seen=post["time"], stated_entry=e,
            entry_low=llm.get("entry_low"), entry_high=llm.get("entry_high"),
            stated_sl=llm.get("stop_loss"), stated_tp=llm.get("take_profit"),
            pips_at_first_sight=stated_pips(llm, direction, ticker),
            image_clock_utc=clock.isoformat() if clock else None,
            post_ids=[post["id"]], last_seen=post["time"],
            confidence=float(llm.get("confidence") or 0), note=llm.get("note", ""),
            repost=repost,
        )
        self.ideas.append(idea)
        return idea, True


def build(rows: list[dict]) -> list[Idea]:
    g = Grouper()
    for r in sorted(rows, key=lambda r: r["post"]["id"]):
        g.add(r["post"], r["llm"])
    return g.ideas
