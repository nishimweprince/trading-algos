"""End-to-end backtest on synthetic data written through the real store."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from conftest import put
from test_lookahead import synthetic

from zone_trend.backtest.engine import RunSpec, run
from zone_trend.backtest.report import write_run
from zone_trend.config import load_config
from zone_trend.data.store import DataPaths, write_month, write_snapshot

MIN = 60_000
H = 3_600_000


def minutes(n_days: int, seed: int) -> pd.DataFrame:
    """1m bars whose hourly resample is the look-ahead suite's synthetic series.

    Each hour walks open -> first extreme (minute 20) -> second extreme
    (minute 40) -> close (minute 59) in straight lines, so hourly OHLC is
    exact and the series is known to produce zone decisions.
    """
    h1 = synthetic(n_days * 24, seed)
    knots_x = np.array([0, 20, 40, 60])
    x = np.arange(61)
    paths = []
    for o, h, lo, c in h1[["open", "high", "low", "close"]].to_numpy():
        first, second = (lo, h) if c >= o else (h, lo)
        paths.append(np.interp(x, knots_x, [o, first, second, c]))
    pts = np.stack(paths)  # (hours, 61)
    open_ = pts[:, :-1].ravel()
    close = pts[:, 1:].ravel()
    n = len(open_)
    vol = np.repeat(h1["volume"].to_numpy() / 60, 60)
    t = np.repeat(h1["open_time"].to_numpy(np.int64), 60) + np.tile(np.arange(60) * MIN, len(h1))
    return pd.DataFrame(
        {
            "open_time": t,
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": vol,
            "close_time": t + MIN - 1,
            "quote_volume": vol * close,
            "count": np.full(n, 5, dtype=np.int64),
            "taker_buy_volume": vol / 2,
            "taker_buy_quote_volume": vol * close / 2,
        }
    )


def frames_from_minutes(m1: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {"1m": m1}
    for tf, ms in (("1h", H), ("4h", 4 * H), ("1d", 24 * H)):
        key = m1["open_time"] // ms * ms
        g = m1.groupby(key)
        df = pd.DataFrame(
            {
                "open": g["open"].first(),
                "high": g["high"].max(),
                "low": g["low"].min(),
                "close": g["close"].last(),
                "volume": g["volume"].sum(),
                "quote_volume": g["quote_volume"].sum(),
                "count": g["count"].sum(),
                "taker_buy_volume": g["taker_buy_volume"].sum(),
                "taker_buy_quote_volume": g["taker_buy_quote_volume"].sum(),
            }
        )
        df["open_time"] = df.index.astype("int64")
        df["close_time"] = df["open_time"] + ms - 1
        out[tf] = df.reset_index(drop=True)
    return out


@pytest.fixture(scope="module")
def synthetic_store(tmp_path_factory):
    paths = DataPaths(tmp_path_factory.mktemp("store") / "data")
    files = []
    for i, sym in enumerate(("AAAUSDT", "BBBUSDT")):
        fr = frames_from_minutes(minutes(500, seed=7 + i))
        for tf, df in fr.items():
            put(paths, "klines", sym, tf, df)
        put(paths, "markPriceKlines", sym, "1m", fr["1m"])
        settle = np.arange(fr["1h"]["open_time"].iloc[0], fr["1h"]["open_time"].iloc[-1], 8 * H)
        funding = pd.DataFrame(
            {
                "calc_time": settle,
                "funding_interval_hours": np.full(len(settle), 8, dtype=np.int64),
                "last_funding_rate": np.full(len(settle), 0.0001),
                "settle_time": settle,
            }
        )
        month = pd.to_datetime(funding["settle_time"], unit="ms", utc=True).dt.strftime("%Y-%m")
        for m, part in funding.groupby(month):
            write_month(
                paths.month_file("fundingRate", sym, "event", m), part, source="monthly", files=1
            )
    files = [(p, 0) for p in paths.parquet.glob("*/*/*/*.parquet")]
    write_snapshot(paths, files, {}, {"symbols": {}})
    return paths


@pytest.mark.parametrize("kind", ["zone", "ema"])
def test_engine_accounting_identity(synthetic_store, tmp_path, kind):
    cfg = load_config()
    spec = RunSpec("2021-08", "2022-05", symbols=["AAAUSDT", "BBBUSDT"], kind=kind, workers=1)
    res = run(synthetic_store, cfg, spec)
    assert len(res.equity) == res.equity["time"].nunique() > 6000
    assert not res.signals.empty
    if not res.meta["open_at_end"]:
        final_cash = res.equity["cash"].iloc[-1]
        assert final_cash == pytest.approx(500 + res.trades["net"].sum(), abs=1e-6)
    if len(res.trades):
        assert (res.trades["exit_time"] >= res.trades["entry_time"]).all()
        assert (res.trades["entry_time"] > res.trades["signal_time"]).all()
    out = write_run(res, cfg, tmp_path / "runs")
    assert (out / "report.html").read_text().startswith("<!doctype html>")
    again = run(synthetic_store, cfg, spec)  # cached tapes, same answer
    assert again.run_id == res.run_id
    pd.testing.assert_frame_equal(again.trades, res.trades)


def test_synthetic_run_trades_enough_to_test_accounting(synthetic_store):
    res = run(
        synthetic_store,
        load_config(),
        RunSpec("2021-08", "2022-05", symbols=["AAAUSDT", "BBBUSDT"], workers=1),
    )
    assert len(res.trades) >= 1 and not res.meta["open_at_end"]
