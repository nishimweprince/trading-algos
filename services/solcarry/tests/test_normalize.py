from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from solcarry.normalize.funding import HOURS_PER_YEAR, binance_hourly, velocity_hourly
from solcarry.normalize.prices import klines_hourly, quarterly_long
from solcarry.normalize.time import epoch_unit, missing_hours, to_utc
from solcarry.normalize.yields import apy_to_continuous, daily_to_hourly
from solcarry.sources.velocity import parse_csv


def test_epoch_units():
    assert epoch_unit(pd.Series([1704067200])) == "s"
    assert epoch_unit(pd.Series([1704067200000])) == "ms"
    assert epoch_unit(pd.Series([1704067200000000])) == "us"


def test_to_utc_mixed_ms_us():
    s = pd.Series([1704067200000, 1735689600000000])
    out = to_utc(s)
    assert out.iloc[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert out.iloc[1] == pd.Timestamp("2025-01-01", tz="UTC")


def test_klines_us_normalized():
    raw = pd.DataFrame(
        {
            "open_time": [1735689600000000, 1735693200000000],
            "open": [1, 2],
            "high": [1, 2],
            "low": [1, 2],
            "close": [1, 2],
        }
    )
    k = klines_hourly(raw)
    assert k.index[0] == pd.Timestamp("2025-01-01 00:00", tz="UTC")
    assert str(k.index.tz) == "UTC"


def test_binance_spread_8h():
    raw = pd.DataFrame(
        {"calc_time": [1704096000000], "funding_interval_hours": [8], "last_funding_rate": [0.0008]}
    )
    out = binance_hourly(raw)
    assert len(out) == 8
    assert out["f_h"].sum() == pytest.approx(0.0008)
    calc = pd.Timestamp(1704096000000, unit="ms", tz="UTC")
    assert out.index.min() == calc - pd.Timedelta(hours=8)
    assert out.index.max() == calc - pd.Timedelta(hours=1)  # hours preceding calc_time


def test_binance_interval_change():
    raw = pd.DataFrame(
        {
            "calc_time": [1704096000000, 1704110400000],
            "funding_interval_hours": [8, 4],
            "last_funding_rate": [0.0008, 0.0004],
        }
    )
    out = binance_hourly(raw)
    assert len(out) == 12
    assert out["f_h"].iloc[-1] == pytest.approx(0.0001)
    assert out["f_h"].sum() == pytest.approx(0.0012)
    assert set(out["interval_h"]) == {8, 4}


def test_binance_calc_time_jitter():
    # calc_time a few ms after the hour still maps to that hour
    raw = pd.DataFrame(
        {"calc_time": [1704096000007], "funding_interval_hours": [8], "last_funding_rate": [0.0008]}
    )
    assert binance_hourly(raw).index.max() == pd.Timestamp("2024-01-01 07:00", tz="UTC")


def test_velocity_conversion_sample_row(fixtures):
    df = parse_csv((fixtures / "velocity_day.csv").read_bytes())
    out = velocity_hourly(df)
    row = out.loc[pd.Timestamp(1790895606, unit="s", tz="UTC").floor("h")]
    assert row["f_h"] == pytest.approx(1.303e-5, rel=1e-3)
    assert row["f_h"] * HOURS_PER_YEAR == pytest.approx(0.114, abs=0.001)
    assert row["f_h"] > 0  # positive = short receives


def test_velocity_duplicate_bucket_summed():
    base = {
        "txSig": "x",
        "txSigIndex": 0,
        "slot": 0,
        "marketIndex": 0,
        "symbol": "SOL-PERP",
        "fundingRateLong": 0.0,
        "fundingRateShort": 0.0,
        "cumulativeFundingRateLong": 0.0,
        "cumulativeFundingRateShort": 0.0,
        "markPriceTwap": 100.0,
        "periodRevenue": 0.0,
        "baseAssetAmountWithAmm": 0.0,
        "baseAssetAmountWithUnsettledLp": 0.0,
    }
    df = pd.DataFrame(
        [
            {
                **base,
                "ts": 1790895606,
                "recordId": 1,
                "fundingRate": 0.001,
                "oraclePriceTwap": 100.0,
            },
            {
                **base,
                "ts": 1790895700,
                "recordId": 2,
                "fundingRate": 0.002,
                "oraclePriceTwap": 100.0,
            },
        ]
    )
    out = velocity_hourly(df)
    assert len(out) == 1
    assert out["f_h"].iloc[0] == pytest.approx(0.00003)
    assert out["n_records"].iloc[0] == 2


def test_apy_continuous():
    assert apy_to_continuous(pd.Series([4.65])).iloc[0] == pytest.approx(np.log(1.0465))


def test_staking_no_ffill_across_long_gap():
    d = pd.Series(
        [0.05, 0.06], index=pd.to_datetime(["2025-01-01 23:00", "2025-01-10 23:00"], utc=True)
    )
    h = daily_to_hourly(d, max_ffill_days=3)
    assert h.loc[pd.Timestamp("2025-01-01 05:00", tz="UTC")] == 0.05
    assert h.loc[pd.Timestamp("2025-01-03 23:00", tz="UTC")] == 0.05
    assert np.isnan(h.loc[pd.Timestamp("2025-01-05 00:00", tz="UTC")])
    assert h.loc[pd.Timestamp("2025-01-10 01:00", tz="UTC")] == 0.06


def test_missing_hours():
    s = pd.Series([1, np.nan, np.nan, 1, np.nan])
    assert missing_hours(s) == (3, 2)


def test_quarterly_long_dte():
    raw = pd.DataFrame(
        {"open_time": [1735689600000], "close": [101.0], "open": 0, "high": 0, "low": 0}
    )
    exp = pd.Timestamp("2025-01-11 00:00", tz="UTC")
    q = quarterly_long({"SOLUSD_250111": raw}, {"SOLUSD_250111": exp})
    assert q["dte_days"].iloc[0] == pytest.approx(10.0)
