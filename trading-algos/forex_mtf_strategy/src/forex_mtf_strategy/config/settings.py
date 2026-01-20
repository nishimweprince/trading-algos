from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyParams:
    # --- Timeframes ---
    base_tf: str = "1H"
    trend_tf: str = "4H"

    # --- Supertrend (trend timeframe) ---
    supertrend_length: int = 10
    supertrend_multiplier: float = 3.0

    # --- StochRSI (base timeframe) ---
    stochrsi_length: int = 14
    stochrsi_rsi_length: int = 14
    stochrsi_k: int = 3
    stochrsi_d: int = 3
    stochrsi_oversold: float = 30.0
    stochrsi_overbought: float = 70.0

    # --- FVG ---
    # Bounce definition: enter zone and close back outside
    require_fvg_bounce: bool = True

    # --- Volume profile (rolling context on base timeframe) ---
    vp_window_bars: int = 240  # ~10 days of 1H bars
    vp_num_bins: int = 48
    vp_hvn_sigma: float = 1.0
    vp_near_level_pips: float = 5.0  # consider "near" HVN/POC within N pips

    # --- Risk / exits (used by backtest strategy) ---
    atr_length: int = 14
    sl_atr_mult: float = 1.5
    tp_rr: float = 2.0


@dataclass(frozen=True)
class BacktestParams:
    cash: float = 10_000.0
    risk_pct: float = 0.01
    spread_pips: float = 1.3
    commission_pct: float = 0.0

