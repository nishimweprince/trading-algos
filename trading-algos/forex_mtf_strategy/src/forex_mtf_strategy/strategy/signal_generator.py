from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from forex_mtf_strategy.config.settings import StrategyParams
from forex_mtf_strategy.data.resampler import forward_fill_to_base, resample_ohlcv
from forex_mtf_strategy.indicators.fvg import compute_fvg, detect_fvg_bounces
from forex_mtf_strategy.indicators.stochrsi import stochrsi_kd
from forex_mtf_strategy.indicators.supertrend import supertrend_direction
from forex_mtf_strategy.indicators.volume_profile import pip_size_for_symbol, volume_profile_context


def generate_signals(
    df_1h: pd.DataFrame,
    *,
    symbol: str | None = None,
    params: StrategyParams | None = None,
) -> pd.DataFrame:
    """
    Generate buy/sell signals on base timeframe candles (typically 1H).
    """
    p = params or StrategyParams()
    df = df_1h.copy()

    # --- Higher timeframe trend filter (4H Supertrend, shifted by 1 candle) ---
    df_4h = resample_ohlcv(df, p.trend_tf)
    st_dir_4h = supertrend_direction(df_4h, length=p.supertrend_length, multiplier=p.supertrend_multiplier)
    df_4h["trend_4h"] = st_dir_4h.shift(1)  # critical: completed HTF candle only
    df["trend_4h"] = forward_fill_to_base(df.index, df_4h["trend_4h"]).astype("Int64")

    # --- Momentum (1H StochRSI) ---
    k_ser, d_ser = stochrsi_kd(
        df["close"],
        length=p.stochrsi_length,
        rsi_length=p.stochrsi_rsi_length,
        k=p.stochrsi_k,
        d=p.stochrsi_d,
    )
    df["stochrsi_k"] = k_ser
    df["stochrsi_d"] = d_ser

    df["from_oversold"] = (
        (df["stochrsi_k"].shift(1) < p.stochrsi_oversold)
        & (df["stochrsi_k"] >= p.stochrsi_oversold)
        & (df["stochrsi_k"] < 60.0)
    )
    df["from_overbought"] = (
        (df["stochrsi_k"].shift(1) > p.stochrsi_overbought)
        & (df["stochrsi_k"] <= p.stochrsi_overbought)
        & (df["stochrsi_k"] > 40.0)
    )

    # --- FVG detection + bounce flags ---
    fvg_df = compute_fvg(df)
    bull_bounce, bear_bounce = detect_fvg_bounces(df, fvg_df)
    df["fvg_bullish_bounce"] = bull_bounce
    df["fvg_bearish_bounce"] = bear_bounce

    # --- Volume profile zones (rolling context) ---
    pip_size = pip_size_for_symbol(symbol)
    df["in_volume_zone"] = volume_profile_context(
        df,
        window_bars=p.vp_window_bars,
        num_bins=p.vp_num_bins,
        hvn_sigma=p.vp_hvn_sigma,
        near_level_pips=p.vp_near_level_pips,
        pip_size=pip_size,
    )

    # --- ATR for stop sizing (used by backtester) ---
    atr = ta.atr(df["high"], df["low"], df["close"], length=p.atr_length)
    df["atr"] = atr

    # --- Signals ---
    fvg_buy_ok = (
        pd.Series(True, index=df.index) if not p.require_fvg_bounce else df["fvg_bullish_bounce"]
    )
    fvg_sell_ok = (
        pd.Series(True, index=df.index) if not p.require_fvg_bounce else df["fvg_bearish_bounce"]
    )

    df["buy_signal"] = (
        (df["trend_4h"] == 1)
        & df["from_oversold"].fillna(False)
        & df["in_volume_zone"].fillna(False)
        & fvg_buy_ok.fillna(False)
    )
    df["sell_signal"] = (
        (df["trend_4h"] == -1)
        & df["from_overbought"].fillna(False)
        & df["in_volume_zone"].fillna(False)
        & fvg_sell_ok.fillna(False)
    )

    return df

