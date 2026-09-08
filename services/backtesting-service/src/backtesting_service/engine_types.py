"""Structures shared between the engine and its strategy modules.

Moved verbatim out of ``engine.py`` so strategy code behind the seam can name
positions, orders and signals without importing the engine (which would close
an import cycle). These are pure data; every behaviour stays where it was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from .models import Candle, EntryMode


def bar_open(bar: Candle, timeframe_minutes: int) -> datetime:
    """Open timestamp of the bar ``bar`` closes."""
    return bar.ts - timedelta(minutes=timeframe_minutes)


@dataclass
class PendingSignal:
    session: str
    range_price: float
    bullish: bool
    signal_ts: datetime
    entry_time: datetime
    anchor_drift_minutes: float = 0.0
    range_high: float | None = None
    range_low: float | None = None


@dataclass
class EntryOrder:
    id: str
    session: str
    mode: EntryMode
    reference_entry: float
    sl_dist: float
    upper_trigger: float
    lower_trigger: float
    bullish: bool
    staged_ts: datetime
    qty: float
    initial_risk_pct: float | None
    initial_risk_cash: float | None
    long_sl: float
    long_tp: float
    short_sl: float
    short_tp: float
    expiry_bars: int | None = None
    bars_seen: int = 0
    reentry_index: int = 0
    root_id: str | None = None


@dataclass
class OrbCollector:
    session: str
    anchor_ts: datetime
    first_open: datetime | None = None
    first_open_px: float | None = None
    high: float | None = None
    low: float | None = None
    last_close: float | None = None
    skipped: bool = False


@dataclass
class EntryLot:
    ts: datetime
    qty: float


@dataclass
class Pair:
    id: str
    session: str
    entry: float
    sl_dist: float
    long_sl: float
    long_tp: float
    short_sl: float
    short_tp: float
    qty: float = 1.0
    long_qty: float | None = None
    short_qty: float | None = None
    long_entry_fills: int = 1
    short_entry_fills: int = 1
    long_episode: int = 0
    short_episode: int = 0
    initial_risk_pct: float | None = None
    initial_risk_cash: float | None = None
    primary_side: Literal["long", "short"] | None = None
    long_open: bool = True
    short_open: bool = True
    locked: bool = False
    entry_ts: datetime = field(default_factory=datetime.now)
    long_entry: float | None = None
    short_entry: float | None = None
    long_entry_ts: datetime | None = None
    short_entry_ts: datetime | None = None
    long_entry_lots: list[EntryLot] = field(default_factory=list)
    short_entry_lots: list[EntryLot] = field(default_factory=list)
    long_mae_pips: float = 0.0
    long_mfe_pips: float = 0.0
    short_mae_pips: float = 0.0
    short_mfe_pips: float = 0.0
    first_close_ts: datetime | None = None
    same_bar_resolved: bool = False
    reference_entry: float | None = None
    entry_gap: bool = False
    exit_gap: bool = False
    entry_ambiguous: bool = False
    entry_bar_close_ts: datetime | None = None
    entry_m1_index: int | None = None
    contingent_initial_ratio: float | None = None
    hedge_failure_threshold: float | None = None
    hedge_ratio_staged: float = 0.0
    hedge_staged: bool = False
    entry_mode: EntryMode = EntryMode.HEDGE_PAIR
    reentry_index: int = 0
    root_id: str | None = None
    bracket_upper: float | None = None
    bracket_lower: float | None = None
    reentry_staged: bool = False
    bullish_signal: bool = True
    long_partial_taken: bool = False
    short_partial_taken: bool = False
    long_be_armed: bool = False
    short_be_armed: bool = False
    survivor_side: Literal["long", "short"] | None = None
    survivor_activated_ts: datetime | None = None
    survivor_post_mae_pips: float = 0.0
    survivor_post_mfe_pips: float = 0.0
    survivor_peak_giveback_pips: float = 0.0
    survivor_ratchet_armed_ts: datetime | None = None
    survivor_ratchet_advances: int = 0


@dataclass
class CostAccounting:
    gross_realized_pips: float = 0.0
    realized_cost_pips: float = 0.0
    gross_unrealized_pips: float = 0.0
    unrealized_cost_pips: float = 0.0
    gross_realized_r: float = 0.0
    realized_cost_r: float = 0.0
    gross_unrealized_r: float = 0.0
    unrealized_cost_r: float = 0.0
    execution_cost_pips: float = 0.0
    financing_cost_pips: float = 0.0
    spread_cost_pips: float = 0.0
    realized_spread_cost_pips: float = 0.0
    transaction_sides: int = 0
    completed_transaction_sides: int = 0
    side_equivalents: float = 0.0
    completed_side_equivalents: float = 0.0
