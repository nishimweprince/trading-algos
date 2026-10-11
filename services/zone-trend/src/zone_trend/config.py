"""Strategy configuration: one YAML file, validated once at startup.

Every section forbids unknown keys and range-checks its values, so a typo or an
out-of-range number stops the bot instead of silently falling back to a default.
Ranges come from the spec's portfolio table where it gives one; elsewhere they
are wide sanity bounds, not tuning limits.

Secrets (Binance keys, notification tokens) are never read from this file; they
come from the environment through the service settings.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from zone_trend.types import TF

__all__ = ["Config", "load_config"]

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _ordered_pair(lo_name: str, pair: tuple[float, float]) -> None:
    if not pair[0] < pair[1]:
        raise ValueError(f"{lo_name}: expected [low, high] with low < high, got {list(pair)}")


class Mode(StrEnum):
    BACKTEST = "backtest"
    SHADOW = "shadow"
    LIVE = "live"


class Exchange(_Section):
    margin_type: Literal["ISOLATED"]
    position_mode: Literal["ONE_WAY"]
    leverage: int = Field(ge=1, le=20)
    fee_maker: float = Field(ge=0, le=0.001)
    fee_taker: float = Field(ge=0, le=0.002)


class Universe(_Section):
    size: int = Field(ge=1, le=30)
    min_listing_days: int = Field(ge=0, le=1000)
    rebalance: Literal["monthly"]
    exclude: list[str] = []
    coins_only: bool = True  # drop TradFi perpetuals (equities, commodities)


class Timeframes(_Section):
    # Exactly [slow, fast], slow-first. The trend TF must equal the fast zone
    # TF, and entry must sit below it, so the core always sees one ordered
    # (slow, fast, entry) ladder. Overridable per-run with ZT_ZONES_TF
    # ("4h,1h"), ZT_TREND_TF and ZT_ENTRY_TF (see load_config).
    zones: list[Literal["5m", "15m", "1h", "4h", "1d"]]
    trend: Literal["5m", "15m", "1h", "4h", "1d"]
    entry: Literal["5m", "15m", "1h", "4h", "1d"]

    @model_validator(mode="after")
    def _ladder(self) -> Self:
        zones = self.zones
        if len(zones) != 2 or zones[0] == zones[1]:
            raise ValueError("timeframes.zones must be exactly [slow, fast]")
        if TF(zones[0]).ms <= TF(zones[1]).ms:
            raise ValueError(f"timeframes.zones must be slow-first, got {zones}")
        if self.trend != zones[1]:
            raise ValueError(
                f"timeframes.trend must equal the fast zone TF ({zones[1]}), got {self.trend}"
            )
        if TF(self.entry).ms >= TF(zones[1]).ms:
            raise ValueError(
                f"timeframes.entry ({self.entry}) must be below the fast zone TF ({zones[1]})"
            )
        return self


class SwingLR(_Section):
    left: int = Field(ge=1, le=20)
    right: int = Field(ge=1, le=20)


class Swings(_Section):
    d1: SwingLR
    h4: SwingLR
    h1: SwingLR
    m15: SwingLR
    m5: SwingLR
    k_atr: float = Field(ge=0, le=5)


class Structure(_Section):
    ema_period: int = Field(ge=5, le=400)
    decay_ratio: float = Field(gt=0, le=1)
    slope_gate: bool = True


class LookbackDays(_Section):
    d1: int = Field(ge=30, le=2000)
    h4: int = Field(ge=7, le=730)
    h1: int = Field(ge=3, le=365)
    m15: int = Field(ge=1, le=90)
    m5: int = Field(ge=1, le=90)

    def days(self, tf: TF) -> int:
        return {
            TF.D1: self.d1,
            TF.H4: self.h4,
            TF.H1: self.h1,
            TF.M15: self.m15,
            TF.M5: self.m5,
        }[tf]


class WidthAtr(_Section):
    min: float = Field(gt=0, le=2)
    max: float = Field(gt=0, le=2)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _ordered_pair("zones.width_atr", (self.min, self.max))
        return self


class Zones(_Section):
    lookback_days: LookbackDays
    width_atr: WidthAtr
    merge_atr_slow: float = Field(ge=0, le=3)
    max_width_atr_slow: float = Field(gt=0, le=5)
    break_atr_fast: float = Field(ge=0, le=3)
    keep_per_side: int = Field(ge=1, le=20)
    use_slow: bool = True
    hvn_lookback_days: int = Field(ge=7, le=730)
    range_min_strength: float = Field(ge=0, le=100)


class Entries(_Section):
    enabled: list[Literal["A", "B", "C"]] = Field(min_length=1)
    breakout_buffer_atr: float = Field(ge=0, le=2)
    min_body_ratio: float = Field(ge=0, le=1)
    volume_mult: float = Field(ge=0, le=10)
    retest_window_bars: int = Field(ge=1, le=500)
    retest_tolerance_atr: float = Field(ge=0, le=2)
    fib_band: tuple[float, float]
    fib_quality_band: tuple[float, float]
    limit_valid_bars: int = Field(ge=1, le=48)
    max_chase_atr: float = Field(ge=0, le=5)
    rejection_min_wick: float = Field(ge=0, le=1)
    rejection_close_frac: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _bands(self) -> Self:
        if set(self.enabled) - {"B"}:
            # Types A and C are off in the spec's defaults and not built yet.
            raise ValueError("entries.enabled: only B is implemented")
        _ordered_pair("entries.fib_band", self.fib_band)
        _ordered_pair("entries.fib_quality_band", self.fib_quality_band)
        return self


class Exits(_Section):
    stop_anchor: Literal["retest", "zone", "further"]
    stop_buffer_atr: float = Field(ge=0, le=3)
    min_stop_atr_entry: float = Field(ge=0, le=5)
    max_stop_atr_fast: float = Field(gt=0, le=10)
    max_stop_pct: float = Field(gt=0, le=0.2)
    tp1_r: float = Field(gt=0, le=10)
    tp1_fraction: float = Field(gt=0, lt=1)
    tp1_from_displacement: bool
    tp2_r: float = Field(gt=0, le=20)
    tp2_rule: Literal["nearer"]
    min_room_r: float = Field(ge=0, le=10)
    trail_atr: float = Field(gt=0, le=10)
    trail_atr_decay: float = Field(gt=0, le=10)
    trail_min_step_atr: float = Field(ge=0, le=5)
    time_stop_bars: int = Field(ge=1, le=2000)
    time_stop_min_r: float = Field(ge=0, le=5)

    @model_validator(mode="after")
    def _targets(self) -> Self:
        if self.tp2_r <= self.tp1_r:
            raise ValueError("exits.tp2_r must exceed exits.tp1_r")
        if self.trail_atr_decay > self.trail_atr:
            raise ValueError("exits.trail_atr_decay tightens the trail; it must be <= trail_atr")
        return self


class Displacement(_Section):
    enabled: bool
    min_trades: int = Field(ge=1, le=1000)
    clamp: tuple[float, float]
    live_weight: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _clamp(self) -> Self:
        _ordered_pair("sizing.displacement.clamp", self.clamp)
        return self


class Sizing(_Section):
    mode: Literal["movement", "fixed_margin"]
    # Above 0.03 is a backtest-only research override (enforced on Config):
    # shadow/live refuse to start with it.
    risk_pct: float = Field(gt=0, le=0.10)
    margin_pct: float = Field(gt=0, le=0.5)
    max_margin_pct: float = Field(gt=0, le=0.5)
    slippage_pct: float = Field(ge=0, le=0.01)
    displacement: Displacement


class Filters(_Section):
    vol_percentiles: tuple[float, float]
    funding_max_8h: float = Field(ge=0, le=0.01)
    funding_blackout_min: int = Field(ge=0, le=120)
    corr_threshold: float = Field(ge=0, le=1)
    corr_window_days: int = Field(ge=5, le=365)
    btc_gate: bool
    mark_last_max_diff: float = Field(gt=0, le=0.1)

    @model_validator(mode="after")
    def _pct(self) -> Self:
        _ordered_pair("filters.vol_percentiles", self.vol_percentiles)
        return self


class Portfolio(_Section):
    max_positions: int = Field(ge=1, le=6)
    max_total_notional_pct: float = Field(ge=0.25, le=1.5)
    max_same_dir_correlated: int = Field(ge=1, le=3)
    daily_loss_pct: float = Field(ge=0.02, le=0.05)
    weekly_loss_pct: float = Field(ge=0.04, le=0.10)
    dd_halve_pct: float = Field(gt=0, lt=1)
    dd_halt_pct: float = Field(gt=0, lt=1)
    consec_losses_halve: int = Field(ge=4, le=10)

    @model_validator(mode="after")
    def _dd(self) -> Self:
        if self.dd_halve_pct >= self.dd_halt_pct:
            raise ValueError("portfolio.dd_halve_pct must be below dd_halt_pct")
        return self


class Live(_Section):
    db_url: str
    reconcile_interval_s: int = Field(ge=5, le=3600)
    stale_stream_s: int = Field(ge=5, le=600)
    weight_budget_frac: float = Field(gt=0, le=1)


class Notify(_Section):
    service: Literal["ta-notify"]
    channels: list[Literal["telegram", "email", "sms", "whatsapp"]]
    heartbeat_min: int = Field(ge=1, le=120)


class Config(_Section):
    mode: Mode
    exchange: Exchange
    universe: Universe
    timeframes: Timeframes
    swings: Swings
    structure: Structure
    zones: Zones
    entries: Entries
    exits: Exits
    sizing: Sizing
    filters: Filters
    portfolio: Portfolio
    live: Live
    notify: Notify

    @model_validator(mode="after")
    def _research_risk(self) -> Self:
        if self.sizing.risk_pct > 0.03 and self.mode is not Mode.BACKTEST:
            raise ValueError("sizing.risk_pct above 0.03 is backtest-only research")
        return self


def load_config(path: Path | str = DEFAULT_PATH) -> Config:
    """Read and validate a config file; raises ``pydantic.ValidationError``.

    The ``timeframes`` section can be overridden per-run without editing the
    file: ``ZT_ZONES_TF`` (slow,fast, e.g. ``4h,1h``), ``ZT_TREND_TF`` and
    ``ZT_ENTRY_TF``. Unset or empty values leave the file untouched.
    """
    with Path(path).open() as fh:
        raw = yaml.safe_load(fh)
    tf = raw.setdefault("timeframes", {})
    if zones := os.environ.get("ZT_ZONES_TF"):
        tf["zones"] = [s.strip() for s in zones.split(",")]
    if trend := os.environ.get("ZT_TREND_TF"):
        tf["trend"] = trend.strip()
    if entry := os.environ.get("ZT_ENTRY_TF"):
        tf["entry"] = entry.strip()
    return Config.model_validate(raw)
