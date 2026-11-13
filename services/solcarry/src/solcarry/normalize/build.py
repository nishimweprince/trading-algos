"""Build all normalized parquet tables from the raw cache."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..config import Config, Paths
from ..http import Http
from ..sources.binance_vision import BinanceVision, Dataset, datasets_for, quarterly_expiry
from ..sources.defillama import DefiLlama
from ..sources.fred import Fred
from ..sources.hyperliquid import Hyperliquid
from ..sources.velocity import Velocity
from .funding import binance_hourly, hyperliquid_hourly, velocity_hourly
from .prices import klines_hourly, quarterly_long
from .time import regrid
from .yields import apy_to_continuous, daily_to_hourly

log = logging.getLogger(__name__)

TABLES = (
    "spot",
    "funding_velocity",
    "velocity_records",
    "funding_binance",
    "index",
    "quarterly",
    "staking",
    "hurdle",
    "lending",
    "expiry_1m",
    "funding_hyperliquid",
)
# Added after the first deploy: an older normalized/ dir may lack these; load them empty.
OPTIONAL_TABLES = ("funding_hyperliquid",)


def _empty_hourly(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="ts"))


@dataclass
class Tables:
    spot: pd.DataFrame
    funding_velocity: pd.DataFrame
    velocity_records: pd.DataFrame
    funding_binance: pd.DataFrame
    index: pd.DataFrame
    quarterly: pd.DataFrame
    staking: pd.DataFrame
    hurdle: pd.DataFrame
    lending: pd.DataFrame
    expiry_1m: pd.DataFrame
    funding_hyperliquid: pd.DataFrame = field(
        default_factory=lambda: _empty_hourly(["f_h", "premium", "n_records"])
    )

    def save(self, out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        for name in TABLES:
            getattr(self, name).to_parquet(out / f"{name}.parquet")

    @classmethod
    def load(cls, out: Path) -> Tables:
        present = {n for n in TABLES if (out / f"{n}.parquet").exists()}
        missing = [n for n in TABLES if n not in present and n not in OPTIONAL_TABLES]
        if missing:
            raise FileNotFoundError(
                f"normalized tables missing: {missing}; run `solcarry normalize` first"
            )
        return cls(**{n: pd.read_parquet(out / f"{n}.parquet") for n in TABLES if n in present})


def _clip(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df
    now = pd.Timestamp.now(tz="UTC").floor("h")
    out = df[df.index <= now]
    if cfg.run.start:
        out = out[out.index >= pd.Timestamp(cfg.run.start, tz="UTC")]
    if cfg.run.end:
        out = out[out.index < pd.Timestamp(cfg.run.end, tz="UTC") + pd.Timedelta(days=1)]
    return out


def build_tables(cfg: Config, paths: Paths) -> Tables:
    http = Http(cfg.http)  # loaders only read the cache; no requests are made
    bv = BinanceVision(cfg, http, paths.raw)
    quarterlies = bv.known_quarterlies()
    ds: dict[str, Dataset] = {d.name: d for d in datasets_for(cfg, quarterlies)}

    spot = _clip(regrid(klines_hourly(bv.load(ds["spot"]))), cfg)
    index = _clip(regrid(klines_hourly(bv.load(ds["index"]), ("close",))), cfg)
    fb = _clip(regrid(binance_hourly(bv.load(ds["funding"]))), cfg)

    per_sym = {q: bv.load(ds[f"quarterly:{q}"]) for q in quarterlies}
    expiries = {q: quarterly_expiry(q, cfg.binance.expiry_hour_utc) for q in quarterlies}
    quarterly = quarterly_long(per_sym, expiries)

    expiry_1m = build_expiry_minutes(bv, quarterlies, expiries)

    vel = Velocity(cfg, http, paths.raw)
    vrec = vel.load()
    fv = _clip(regrid(velocity_hourly(vrec)), cfg)

    fhl = _clip(regrid(hyperliquid_hourly(Hyperliquid(cfg, http, paths.raw).load())), cfg)

    dl = DefiLlama(cfg, http, paths.raw)
    now = pd.Timestamp.now(tz="UTC").floor("h")
    st = dl.load("jitosol")
    y_lst = (
        daily_to_hourly(
            pd.Series(apy_to_continuous(st["apyBase"]).to_numpy(), index=st["timestamp"]),
            cfg.staking.max_ffill_days,
            end=now,
        )
        if not st.empty
        else pd.Series(dtype="float64")
    )
    staking = pd.DataFrame({"y_lst": y_lst})
    staking["y_native"] = staking["y_lst"] / (1.0 - cfg.staking.pool_fee)
    staking.index.name = "ts"
    staking = _clip(staking, cfg)

    lend_cols = {}
    for name, col in (("aave_usdc", "r_res_aave"), ("kamino_usdc", "r_res_kamino")):
        d = dl.load(name)
        if not d.empty:
            lend_cols[col] = daily_to_hourly(
                pd.Series(apy_to_continuous(d["apyBase"]).to_numpy(), index=d["timestamp"]),
                cfg.staking.max_ffill_days,
                end=now,
            )
    lending = pd.DataFrame(lend_cols)
    lending.index.name = "ts"
    lending = _clip(lending, cfg)

    fred = Fred(cfg, http, paths.raw).load()
    if not fred.empty:
        fred = fred[fred["date"] >= pd.Timestamp("2019-01-01", tz="UTC")]
    series = cfg.sources.fred_series
    if fred.empty:
        hurdle = pd.DataFrame(columns=["r_f"], index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    else:
        rf = apy_to_continuous(fred[series])
        # Weekends/holidays are '.'; bridge up to 7 days (long weekends + data lag).
        hurdle = pd.DataFrame(
            {"r_f": daily_to_hourly(pd.Series(rf.to_numpy(), index=fred["date"]), 7, end=now)}
        )
        hurdle = _clip(hurdle, cfg)

    http.close()
    return Tables(
        spot=spot,
        funding_velocity=fv,
        velocity_records=vrec,
        funding_binance=fb,
        index=index,
        quarterly=quarterly,
        staking=staking,
        hurdle=hurdle,
        lending=lending,
        expiry_1m=expiry_1m,
        funding_hyperliquid=fhl,
    )


EXPIRY_1M_COLS = ["ts", "symbol", "expiry_ts", "f_close", "f_volume", "i_close"]


def build_expiry_minutes(
    bv: BinanceVision, quarterlies: list[str], expiries: dict[str, pd.Timestamp]
) -> pd.DataFrame:
    """Minute bars of contract and index from expiry-2h to expiry+1h, per expired contract."""
    from .time import to_utc

    frames = []
    for q in quarterlies:
        fkey, ikey = bv.expiry_minute_keys(q)
        f, i = bv.load_key(fkey), bv.load_key(ikey)
        if f.empty or i.empty:
            continue
        exp = expiries[q]
        lo, hi = exp - pd.Timedelta(hours=2), exp + pd.Timedelta(hours=1)
        f = f.assign(ts=to_utc(f["open_time"])).drop_duplicates("ts").set_index("ts")
        i = i.assign(ts=to_utc(i["open_time"])).drop_duplicates("ts").set_index("ts")
        f = f[(f.index >= lo) & (f.index < hi)]
        frames.append(
            pd.DataFrame(
                {
                    "ts": f.index,
                    "symbol": q,
                    "expiry_ts": exp,
                    "f_close": pd.to_numeric(f["close"]).to_numpy(),
                    "f_volume": pd.to_numeric(f["volume"]).to_numpy(),
                    "i_close": pd.to_numeric(i["close"]).reindex(f.index).to_numpy(),
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=EXPIRY_1M_COLS)
    return pd.concat(frames, ignore_index=True)
