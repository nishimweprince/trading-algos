"""Instrument specifications for position sizing and SL/TP calculation."""
from enum import Enum
from dataclasses import dataclass
from typing import Dict
from loguru import logger


class InstrumentType(Enum):
    FX = "FX"
    INDEX = "INDEX"
    COMMODITY = "COMMODITY"
    CRYPTO = "CRYPTO"
    SHARE = "SHARE"


@dataclass(frozen=True)
class InstrumentSpec:
    """Static metadata for a tradeable instrument on Capital.com.

    Attributes:
        instrument_type: Classification (FX, INDEX, etc.)
        tick_size: Minimum price increment (e.g. 0.00001 for EUR/USD, 0.01 for VIX)
        value_per_point: PnL per 1.0 price move per 1 contract/lot.
            FX: 100_000 (1 standard lot, $1 move = $100k). Index/commodity/crypto CFDs: 1.0.
        sl_atr_mult: Stop loss distance as ATR multiple.
        tp_atr_mult: Take profit distance as ATR multiple.
        min_size: Minimum order size on Capital.com.
        spread_cost: Typical spread in price units (used in backtest).
    """
    instrument_type: InstrumentType
    tick_size: float
    value_per_point: float
    sl_atr_mult: float
    tp_atr_mult: float
    min_size: float
    spread_cost: float


# ---------------------------------------------------------------------------
# Instrument registry
# ---------------------------------------------------------------------------

INSTRUMENT_SPECS: Dict[str, InstrumentSpec] = {
    # ── FX Majors ──────────────────────────────────────────────────────────
    'EUR_USD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00015),
    'GBP_USD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00020),
    'AUD_USD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00018),
    'NZD_USD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00020),
    'USD_CHF': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00018),
    'USD_CAD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00020),
    'USD_JPY': InstrumentSpec(InstrumentType.FX, tick_size=0.001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.015),

    # ── FX Crosses ─────────────────────────────────────────────────────────
    'EUR_GBP': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00020),
    'EUR_JPY': InstrumentSpec(InstrumentType.FX, tick_size=0.001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.020),
    'GBP_JPY': InstrumentSpec(InstrumentType.FX, tick_size=0.001, value_per_point=100_000,
                              sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.030),
    'AUD_JPY': InstrumentSpec(InstrumentType.FX, tick_size=0.001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.020),
    'EUR_CHF': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00020),
    'EUR_AUD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00025),
    'GBP_AUD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.00030),
    'EUR_CAD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00025),
    'GBP_CAD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.00030),
    'AUD_NZD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00025),
    'CHF_JPY': InstrumentSpec(InstrumentType.FX, tick_size=0.001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.020),
    'CAD_JPY': InstrumentSpec(InstrumentType.FX, tick_size=0.001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.020),
    'NZD_JPY': InstrumentSpec(InstrumentType.FX, tick_size=0.001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.020),
    'GBP_CHF': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.00025),
    'AUD_CAD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00025),
    'AUD_CHF': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00025),
    'NZD_CAD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00025),
    'NZD_CHF': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00025),
    'EUR_NZD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00030),
    'GBP_NZD': InstrumentSpec(InstrumentType.FX, tick_size=0.00001, value_per_point=100_000,
                              sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.00035),

    # ── Indices ────────────────────────────────────────────────────────────
    'SP500':   InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=0.5),
    'US500':   InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=0.5),
    'NASDAQ':  InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=1.0),
    'US100':   InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=1.0),
    'DOW':     InstrumentSpec(InstrumentType.INDEX, tick_size=1.0, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=2.0),
    'US30':    InstrumentSpec(InstrumentType.INDEX, tick_size=1.0, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=2.0),
    'VIX':     InstrumentSpec(InstrumentType.INDEX, tick_size=0.01, value_per_point=1.0,
                              sl_atr_mult=3.0, tp_atr_mult=5.0, min_size=0.1, spread_cost=0.05),
    'DAX':     InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=1.0),
    'DE40':    InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=1.0),
    'FTSE100': InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=1.0),
    'UK100':   InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=1.0),
    'NIKKEI225': InstrumentSpec(InstrumentType.INDEX, tick_size=1.0, value_per_point=1.0,
                                sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=10.0),
    'J225':    InstrumentSpec(InstrumentType.INDEX, tick_size=1.0, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=10.0),
    'RUSSELL2000': InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                                  sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=0.5),
    'RTY':     InstrumentSpec(InstrumentType.INDEX, tick_size=0.1, value_per_point=1.0,
                              sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=0.5),

    # ── Commodities ────────────────────────────────────────────────────────
    'XAU_USD':    InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.01, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.30),
    'GOLD':       InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.01, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.30),
    'XAG_USD':    InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.001, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.020),
    'SILVER':     InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.001, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.020),
    'CRUDE_OIL':  InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.01, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.1, spread_cost=0.04),
    'WTI':        InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.01, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.1, spread_cost=0.04),
    'OIL_WTI':    InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.01, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.1, spread_cost=0.04),
    'BRENT':      InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.01, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.1, spread_cost=0.04),
    'OIL_BRENT':  InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.01, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.1, spread_cost=0.04),
    'NATURAL_GAS': InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.001, value_per_point=1.0,
                                  sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=0.1, spread_cost=0.005),
    'COPPER':     InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.001, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.005),
    'PLATINUM':   InstrumentSpec(InstrumentType.COMMODITY, tick_size=0.01, value_per_point=1.0,
                                 sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=1.0),

    # ── Crypto ─────────────────────────────────────────────────────────────
    'BTC_USD':  InstrumentSpec(InstrumentType.CRYPTO, tick_size=0.01, value_per_point=1.0,
                               sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=0.01, spread_cost=50.0),
    'ETH_USD':  InstrumentSpec(InstrumentType.CRYPTO, tick_size=0.01, value_per_point=1.0,
                               sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=0.01, spread_cost=3.0),
    'SOL_USD':  InstrumentSpec(InstrumentType.CRYPTO, tick_size=0.01, value_per_point=1.0,
                               sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=0.1, spread_cost=0.20),
    'XRP_USD':  InstrumentSpec(InstrumentType.CRYPTO, tick_size=0.0001, value_per_point=1.0,
                               sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=1.0, spread_cost=0.005),
    'ADA_USD':  InstrumentSpec(InstrumentType.CRYPTO, tick_size=0.0001, value_per_point=1.0,
                               sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=1.0, spread_cost=0.003),
    'DOGE_USD': InstrumentSpec(InstrumentType.CRYPTO, tick_size=0.00001, value_per_point=1.0,
                               sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=10.0, spread_cost=0.001),

    # ── Shares ─────────────────────────────────────────────────────────────
    'AAPL':  InstrumentSpec(InstrumentType.SHARE, tick_size=0.01, value_per_point=1.0,
                            sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=1.0, spread_cost=0.10),
    'TSLA':  InstrumentSpec(InstrumentType.SHARE, tick_size=0.01, value_per_point=1.0,
                            sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=1.0, spread_cost=0.20),
    'NVDA':  InstrumentSpec(InstrumentType.SHARE, tick_size=0.01, value_per_point=1.0,
                            sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=1.0, spread_cost=0.15),
    'MSFT':  InstrumentSpec(InstrumentType.SHARE, tick_size=0.01, value_per_point=1.0,
                            sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=1.0, spread_cost=0.10),
    'GOOGL': InstrumentSpec(InstrumentType.SHARE, tick_size=0.01, value_per_point=1.0,
                            sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=1.0, spread_cost=0.15),
    'AMZN':  InstrumentSpec(InstrumentType.SHARE, tick_size=0.01, value_per_point=1.0,
                            sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=1.0, spread_cost=0.15),
    'META':  InstrumentSpec(InstrumentType.SHARE, tick_size=0.01, value_per_point=1.0,
                            sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=1.0, spread_cost=0.15),
}

# ---------------------------------------------------------------------------
# Type-level defaults for unknown instruments
# ---------------------------------------------------------------------------

_TYPE_DEFAULTS = {
    InstrumentType.FX:        dict(tick_size=0.00001, value_per_point=100_000, sl_atr_mult=1.5, tp_atr_mult=3.0, min_size=0.01, spread_cost=0.00020),
    InstrumentType.INDEX:     dict(tick_size=0.1, value_per_point=1.0, sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=0.1, spread_cost=1.0),
    InstrumentType.COMMODITY: dict(tick_size=0.01, value_per_point=1.0, sl_atr_mult=2.0, tp_atr_mult=3.5, min_size=0.01, spread_cost=0.10),
    InstrumentType.CRYPTO:    dict(tick_size=0.01, value_per_point=1.0, sl_atr_mult=2.5, tp_atr_mult=4.0, min_size=0.01, spread_cost=1.0),
    InstrumentType.SHARE:     dict(tick_size=0.01, value_per_point=1.0, sl_atr_mult=2.0, tp_atr_mult=3.0, min_size=1.0, spread_cost=0.10),
}

_KNOWN_INDICES = {
    'SP500', 'US500', 'SPX', 'NASDAQ', 'US100', 'NDX', 'DOW', 'US30', 'DJI',
    'RUSSELL2000', 'RTY', 'VIX', 'DAX', 'DE40', 'FTSE100', 'UK100', 'CAC40',
    'FR40', 'EUROSTOXX50', 'EU50', 'NIKKEI225', 'J225', 'HANGSENG', 'HK50',
    'CHINAA50', 'CN50', 'ASX200', 'AU200',
}

_KNOWN_COMMODITIES = {
    'XAU_USD', 'XAG_USD', 'GOLD', 'SILVER', 'CRUDE_OIL', 'WTI', 'OIL_WTI',
    'BRENT', 'OIL_BRENT', 'NATURAL_GAS', 'NATGAS', 'COPPER', 'PLATINUM', 'PALLADIUM',
}

_KNOWN_CRYPTO = {
    'BTC_USD', 'ETH_USD', 'SOL_USD', 'XRP_USD', 'ADA_USD', 'DOGE_USD',
    'LTC_USD', 'DOT_USD', 'AVAX_USD', 'LINK_USD', 'MATIC_USD',
    'BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'ADAUSD', 'DOGEUSD',
}


def _infer_instrument_type(instrument: str) -> InstrumentType:
    """Heuristic: infer type from instrument name format."""
    upper = instrument.upper()
    if upper in _KNOWN_INDICES:
        return InstrumentType.INDEX
    if upper in _KNOWN_COMMODITIES:
        return InstrumentType.COMMODITY
    if upper in _KNOWN_CRYPTO:
        return InstrumentType.CRYPTO
    # FX pairs: XXX_YYY where both parts are 3-letter alpha
    if '_' in upper:
        parts = upper.split('_')
        if len(parts) == 2 and all(len(p) == 3 and p.isalpha() for p in parts):
            return InstrumentType.FX
    return InstrumentType.SHARE


def get_instrument_spec(instrument: str) -> InstrumentSpec:
    """Look up spec for an instrument, with fallback to type-based defaults."""
    spec = INSTRUMENT_SPECS.get(instrument)
    if spec is not None:
        return spec

    inferred_type = _infer_instrument_type(instrument)
    defaults = _TYPE_DEFAULTS[inferred_type]
    logger.warning(f"No spec for '{instrument}', using {inferred_type.value} defaults")
    return InstrumentSpec(instrument_type=inferred_type, **defaults)
