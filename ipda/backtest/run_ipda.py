"""Backtest the IPDA model (src/ipda_model.py, docs/ipda-model.md) on XAUUSD M1.

Every minute is streamed through the same IpdaModel the live service runs. Setups become
limit orders that expire at the killzone end; fills, stops and targets are simulated on
1-minute bid bars with ask = bid + spread.

Usage:  uv run --no-sync python run_ipda.py [--spread 0.2] [--open]
Writes results/ipda_results.json and results/ipda_report.html.
"""

from __future__ import annotations

import argparse
import json
import sys
import time as _time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # `src` as a package, exactly the live code

from data import load_minutes  # noqa: E402
from engine import ExitParams, _walk  # noqa: E402
from run_backtest import IS_END, OUT, _clean, _exp, breakdowns, render, summary  # noqa: E402

from src.candles import Candle  # noqa: E402
from src.ipda_model import DEFAULT_KILLZONES, IpdaModel, IpdaParams  # noqa: E402

PIP = 0.10
PRIMARY = IpdaParams(min_risk=10 * PIP, max_risk=150 * PIP)

VARIANTS: list[tuple[str, str, IpdaParams]] = [
    (
        "ipda_v1",
        "Pre-registered v1: 20-day range PD filter, Asia/PD liquidity sweep in London + "
        "NY AM killzones, 3M fractal shift, limit at FVG midpoint, stop at sweep, 2R",
        PRIMARY,
    ),
    (
        "no_pd_filter",
        "Exploratory: v1 without the premium/discount filter",
        replace(PRIMARY, pd_filter=False),
    ),
    (
        "liquidity_target",
        "Exploratory: target = nearest Asian/previous-day pool at least 1R away",
        replace(PRIMARY, target_mode="liquidity"),
    ),
    (
        "london_only",
        "Exploratory: London killzone only",
        replace(PRIMARY, killzones=(DEFAULT_KILLZONES[0],)),
    ),
    (
        "ny_am_only",
        "Exploratory: NY AM killzone only",
        replace(PRIMARY, killzones=(DEFAULT_KILLZONES[1],)),
    ),
    ("rr_1_5", "Exploratory: target 1.5R", replace(PRIMARY, risk_reward=1.5)),
]


def stream_setups(params: IpdaParams) -> tuple[pd.DataFrame, dict]:
    df = load_minutes()
    starts = df["ts"].array.to_pydatetime()
    model = IpdaModel(params)
    rows = []
    cols = (df["open"].tolist(), df["high"].tolist(), df["low"].tolist(), df["close"].tolist())
    for i, (s, o, h, lo, c) in enumerate(zip(starts, *cols, strict=True)):
        setup = model.on_minute(Candle(s, o, h, lo, c, 0.0))
        if setup is not None:
            rows.append(
                {
                    "i": i,
                    "dir": 1 if setup.direction == "buy" else -1,
                    "entry": setup.entry,
                    "stop": setup.stop_loss,
                    "tp": setup.take_profit,
                    "created": setup.created_at,
                    "expires": setup.expires_at,
                    "session": setup.killzone,
                    "eq": setup.equilibrium,
                }
            )
    return pd.DataFrame(rows), dict(model.stats)


def simulate_limits(df: pd.DataFrame, setups: pd.DataFrame, spread: float) -> pd.DataFrame:
    OP, H, L, C = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    ts_ns = df["ts"].dt.as_unit("ns").astype("int64").to_numpy()
    n = len(C)
    ex = ExitParams(spread=spread)
    out = []
    for r in setups.itertuples():
        d, entry = r.dir, r.entry
        end = int(np.searchsorted(ts_ns, pd.Timestamp(r.expires).value))  # minutes before expiry
        a, b = r.i + 1, min(end, n)
        if a >= b:
            continue
        if d == 1:  # buy limit: ask = bid + spread trades down to the entry
            hit = L[a:b] + spread <= entry
        else:  # sell limit: bid trades up to the entry
            hit = H[a:b] >= entry
        if not hit.any():
            continue
        f = a + int(np.argmax(hit))
        side_open = OP[f] + spread if d == 1 else OP[f]
        fill = min(entry, side_open) if d == 1 else max(entry, side_open)
        risk = abs(r.entry - r.stop)
        # the fill minute itself: a stop touched there counts (pessimistic)
        stop_hit = (L[f] <= r.stop) if d == 1 else (H[f] + spread >= r.stop)
        if stop_hit:
            x, px, reason = f, r.stop, "sl"
        else:
            res = _walk(
                OP, H, L, C, n, f, d, fill, abs(fill - r.stop), abs(r.tp - fill), spread, ex
            )
            if res is None:
                continue
            x, px, reason = res
        pnl = (px - fill) if d == 1 else (fill - px)
        out.append(
            {
                "ts": int(ts_ns[f]) + 60_000_000_000,
                "created": r.created,
                "dir": d,
                "session": r.session,
                "entry_px": fill,
                "exit_px": px,
                "reason": reason,
                "pnl": pnl,
                "r": pnl / risk,
                "sl_dist": risk,
                "exit_ts": int(ts_ns[x]) + 60_000_000_000,
            }
        )
    t = pd.DataFrame(out)
    if not t.empty:
        t["ts"] = pd.to_datetime(t["ts"], utc=True)
        t["exit_ts"] = pd.to_datetime(t["exit_ts"], utc=True)
        t["hold_min"] = (t["exit_ts"] - t["ts"]).dt.total_seconds() / 60
    return t


def _ci(t: pd.DataFrame) -> tuple[float, float] | None:
    if len(t) < 2:
        return None
    m, se = t["r"].mean(), t["r"].std() / np.sqrt(len(t))
    return round(float(m - 1.96 * se), 4), round(float(m + 1.96 * se), 4)


def _verdict(t: pd.DataFrame) -> dict:
    ins, oos = t[t["ts"] < IS_END], t[t["ts"] >= IS_END]
    ci = _ci(oos)
    passed = bool(
        len(ins)
        and len(oos)
        and ins["r"].mean() > 0
        and oos["r"].mean() > 0
        and ci
        and ci[0] > -0.05
    )
    return {
        "is_exp_r": _exp(ins),
        "oos_exp_r": _exp(oos),
        "oos_ci95": ci,
        "is_ci95": _ci(ins),
        "pass": passed,
    }


def _job(item):
    key, desc, params = item
    t0 = _time.time()
    setups, stats = stream_setups(params)
    return key, desc, setups, stats, _time.time() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spread", type=float, default=0.20)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()
    t0 = _time.time()
    df = load_minutes()
    with ProcessPoolExecutor(max_workers=min(6, len(VARIANTS))) as pool:
        streamed = list(pool.map(_job, VARIANTS))

    variants, funnel = [], {}
    for key, desc, setups, stats, secs in streamed:
        t = simulate_limits(df, setups, args.spread)
        s = summary(t)
        funnel[key] = {**stats, "orders": len(setups), "filled": len(t)}
        print(
            f"{key:<17} setups={len(setups):>5} filled={len(t):>5} exp={s.get('exp_r')}R "
            f"IS={s.get('is_exp_r')} OOS={s.get('oos_exp_r')} net=${s.get('net_usd')} "
            f"({secs:.0f}s)"
        )
        variants.append(
            {
                "key": key,
                "desc": desc,
                "summary": s,
                **breakdowns(t, None),
                "verdict": _verdict(t),
                "_t": t,
                "_setups": setups,
            }
        )

    primary = variants[0]
    spreads = []
    for sp in (0.0, 0.1, 0.2, 0.3, 0.4, 0.6):
        t = simulate_limits(df, primary["_setups"], sp)
        spreads.append({"spread": sp, "exp_r": _exp(t), "net_usd": round(float(t["pnl"].sum()), 2)})

    v = primary["verdict"]
    print(
        f"\nPRE-REGISTERED VERDICT (ipda_v1): {'PASS' if v['pass'] else 'FAIL'}  "
        f"IS {v['is_exp_r']} {v['is_ci95']}  OOS {v['oos_exp_r']} {v['oos_ci95']}"
    )

    tail = primary["_t"].tail(400)
    result = {
        "title": "IPDA model (ICT) on XAUUSD: backtest",
        "generated": pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "data": {
            "from": str(df["ts"].iloc[0])[:16],
            "to": str(df["ts"].iloc[-1])[:16],
            "minutes": len(df),
            "source": "HistData XAUUSD M1 bid (NY local time)",
        },
        "verdict": {
            "key": "ipda_v1",
            **v,
            "rule": "PASS needs IS exp > 0, OOS exp > 0 and OOS 95% CI low > -0.05R",
        },
        "assumptions": [
            "Same IpdaModel class as the live service; rules in docs/ipda-model.md (v1).",
            f"Limit orders: a buy fills when bid + ${args.spread:.2f} ≤ entry, a sell when "
            "bid ≥ entry; expiry at killzone end; no commission.",
            "A stop touched on the fill minute counts as a stop; afterwards a minute "
            "touching both levels counts as a stop.",
            "Risk 10-150 pips ($1-$15). $ figures per 0.01 lot (1 oz); R = P&L / planned risk.",
            "In-sample 2009-2019, out-of-sample 2020 onward. Only ipda_v1 is pre-registered.",
        ],
        "funnel": funnel,
        "variants": [{k: x for k, x in var.items() if not k.startswith("_")} for var in variants],
        "atr_grid": [],
        "spreads": spreads,
        "trades": [
            {
                "ts": r.ts.strftime("%Y-%m-%d %H:%M"),
                "dir": "long" if r.dir == 1 else "short",
                "session": r.session,
                "entry": round(r.entry_px, 2),
                "exit": round(r.exit_px, 2),
                "reason": r.reason,
                "pnl": round(r.pnl, 2),
                "r": round(r.r, 2),
                "hold": round(r.hold_min),
                "rsi": None,
            }
            for r in tail.itertuples()
        ],
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "ipda_results.json").write_text(json.dumps(_clean(result), allow_nan=False))
    print(f"done in {_time.time() - t0:.0f}s")
    render(args.open, stem="ipda_")


if __name__ == "__main__":
    main()
