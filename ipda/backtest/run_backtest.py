"""Backtest the IPDA reversal entry on XAUUSD M1 and write results.json + report.html.

Defaults mirror the FTMO gold profile (.env.example.ftmo + symbols.example.ftmo.json):
3M forming-bar RSI(14) 25/75 cross, Tokyo + New York sessions, SL 40 pips / TP 50 pips
at pip 0.10 (= $4 / $5), 0.01 lot (1 oz, so $ figures are per 0.01 lot).

Usage:  uv run --no-sync python run_backtest.py [--spread 0.2] [--open]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time as _time
from pathlib import Path

import numpy as np
import pandas as pd
from data import load_minutes
from engine import ExitParams, build_buckets, reversal_signals, session_labels, simulate

HERE = Path(__file__).resolve().parent
OUT = HERE / "results"
IS_END = pd.Timestamp("2020-01-01", tz="UTC")  # in-sample 2009-2019, out-of-sample 2020+

PIP = 0.10
SL_PIPS, TP_PIPS = 40, 50
SESSIONS = ["tokyo", "new_york"]


# --------------------------------------------------------------------------- metrics
def _dd(cum: np.ndarray) -> float:
    if len(cum) == 0:
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    return float((cum - peak).min())


def summary(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    pnl, r = t["pnl"].to_numpy(), t["r"].to_numpy()
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    daily = t.groupby(t["exit_ts"].dt.floor("D"))["r"].sum()
    days = pd.date_range(daily.index.min(), daily.index.max(), freq="B", tz="UTC")
    daily = daily.reindex(days, fill_value=0.0)
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    years = max((t["exit_ts"].max() - t["ts"].min()).days / 365.25, 1e-9)
    cum_r = np.cumsum(r)
    return {
        "trades": int(len(t)),
        "per_year": round(len(t) / years, 1),
        "win_rate": round(float((pnl > 0).mean()), 4),
        "net_usd": round(float(pnl.sum()), 2),
        "net_r": round(float(r.sum()), 1),
        "exp_r": round(float(r.mean()), 4),
        "exp_usd": round(float(pnl.mean()), 3),
        "pf": round(float(wins / losses), 3) if losses > 0 else None,
        "max_dd_r": round(_dd(cum_r), 1),
        "max_dd_usd": round(_dd(np.cumsum(pnl)), 2),
        "sharpe": round(sharpe, 2),
        "avg_hold_min": round(float(t["hold_min"].mean()), 1),
        "med_hold_min": round(float(t["hold_min"].median()), 1),
        "tp_rate": round(float((t["reason"] == "tp").mean()), 4),
        "is_exp_r": _exp(t[t["ts"] < IS_END]),
        "oos_exp_r": _exp(t[t["ts"] >= IS_END]),
        "is_trades": int((t["ts"] < IS_END).sum()),
        "oos_trades": int((t["ts"] >= IS_END).sum()),
    }


def _exp(t: pd.DataFrame) -> float | None:
    return round(float(t["r"].mean()), 4) if len(t) else None


def _group(t: pd.DataFrame, key, label=str) -> list[dict]:
    out = []
    for k, g in t.groupby(key, sort=True):
        out.append(
            {
                "key": label(k),
                "n": int(len(g)),
                "net_r": round(float(g["r"].sum()), 1),
                "net_usd": round(float(g["pnl"].sum()), 2),
                "exp_r": round(float(g["r"].mean()), 4),
                "win": round(float((g["pnl"] > 0).mean()), 4),
            }
        )
    return out


def breakdowns(t: pd.DataFrame, atr_year: pd.Series) -> dict:
    eq = t.sort_values("exit_ts")
    day = eq.groupby(eq["exit_ts"].dt.floor("D")).agg(r=("r", "sum"), usd=("pnl", "sum"))
    cum_r, cum_usd = day["r"].cumsum(), day["usd"].cumsum()
    dd_r = cum_r - np.maximum.accumulate(np.maximum(cum_r.to_numpy(), 0))
    years = _group(t, t["ts"].dt.year)
    for y in years:
        atr = np.nan if atr_year is None else atr_year.get(int(y["key"]), np.nan)
        y["atr"] = round(float(atr), 2)
    monthly = t.groupby([t["ts"].dt.year, t["ts"].dt.month])["r"].sum()
    return {
        "equity": {
            "d": [d.strftime("%Y-%m-%d") for d in day.index],
            "r": np.round(cum_r.to_numpy(), 2).tolist(),
            "usd": np.round(cum_usd.to_numpy(), 2).tolist(),
            "dd": np.round(dd_r, 2).tolist(),
        },
        "year": years,
        "month": [
            {"y": int(y), "m": int(m), "r": round(float(v), 1)} for (y, m), v in monthly.items()
        ],
        "hour": _group(t, t["ts"].dt.hour, lambda h: f"{int(h):02d}"),
        "session": _group(t, t["session"]),
        "dir": _group(t, t["dir"], lambda d: "long" if d == 1 else "short"),
        "reason": _group(t, t["reason"]),
        "minute": _group(t, t["minute_in_bucket"], lambda m: f"min {int(m) + 1}")
        if "minute_in_bucket" in t
        else [],
        "weekday": _group(t, t["ts"].dt.weekday, lambda w: "MTWTFSS"[int(w)] + str(int(w))),
    }


# --------------------------------------------------------------------------- variants
def run_variant(df, sig, *, sl, tp, ex: ExitParams, sessions=SESSIONS, mask=None):
    s = sig.copy()
    s["session"] = session_labels(s["ts"], sessions) if sessions else "always"
    keep = (s["session"] != "") & s["atr"].notna()
    if mask is not None:
        keep &= mask
    s = s[keep.to_numpy()].reset_index(drop=True)
    sl_a = sl(s) if callable(sl) else np.full(len(s), sl)
    tp_a = tp(s) if callable(tp) else np.full(len(s), tp)
    return simulate(df, s, sl_a, tp_a, ex)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spread", type=float, default=0.20, help="$ per oz round trip (ask-bid)")
    ap.add_argument("--open", action="store_true", help="open report.html when done")
    ap.add_argument(
        "--render-only", action="store_true", help="rebuild report.html from results.json"
    )
    args = ap.parse_args()
    if args.render_only:
        render(args.open)
        return
    t0 = _time.time()

    df = load_minutes()
    b = build_buckets(df)
    sig_f = reversal_signals(df, b, mode="forming")
    sig_c = reversal_signals(df, b, mode="close")
    atr_year = (
        pd.Series(b.atr, index=pd.DatetimeIndex(df["ts"].iloc[b.last_minute]))
        .groupby(lambda x: x.year)
        .median()
    )
    print(
        f"loaded {len(df):,} minutes, {len(sig_f):,} raw forming signals ({_time.time() - t0:.0f}s)"
    )

    base_ex = ExitParams(spread=args.spread)
    SL, TP = SL_PIPS * PIP, TP_PIPS * PIP
    variants: list[tuple[str, str, pd.DataFrame]] = []

    def add(key, desc, trades):
        variants.append((key, desc, trades))
        s = summary(trades)
        print(
            f"{key:<22} n={s['trades']:>6} exp={s.get('exp_r', 0):+.3f}R "
            f"IS={s.get('is_exp_r')} OOS={s.get('oos_exp_r')} net=${s.get('net_usd')}"
        )

    base = run_variant(df, sig_f, sl=SL, tp=TP, ex=base_ex)
    add(
        "baseline",
        "Default FTMO gold profile: 3M forming-bar RSI14 25/75, Tokyo+NY, "
        f"SL $4 / TP $5, spread ${args.spread:.2f}, same-bar SL first",
        base,
    )
    add(
        "same_bar_tp",
        "Baseline but a bar touching both levels counts as TP (optimistic bound)",
        run_variant(df, sig_f, sl=SL, tp=TP, ex=ExitParams(spread=args.spread, same_bar="tp")),
    )
    add(
        "zero_spread",
        "Baseline with zero spread (gross edge of the signal)",
        run_variant(df, sig_f, sl=SL, tp=TP, ex=ExitParams(spread=0.0)),
    )
    add(
        "close_confirmed",
        "Signal only at 3M bar close (TradingView alert timing)",
        run_variant(df, sig_c, sl=SL, tp=TP, ex=base_ex),
    )
    add(
        "all_hours",
        "No session filter (24h)",
        run_variant(df, sig_f, sl=SL, tp=TP, ex=base_ex, sessions=[]),
    )
    add(
        "tokyo_only",
        "Tokyo session only",
        run_variant(df, sig_f, sl=SL, tp=TP, ex=base_ex, sessions=["tokyo"]),
    )
    add(
        "ny_only",
        "New York session only",
        run_variant(df, sig_f, sl=SL, tp=TP, ex=base_ex, sessions=["new_york"]),
    )
    add(
        "break_even_30",
        "Operator follows the MFE advisory: SL to entry at +30 pips",
        run_variant(
            df, sig_f, sl=SL, tp=TP, ex=ExitParams(spread=args.spread, be_trigger=30 * PIP)
        ),
    )
    add(
        "inverse",
        "Diagnostic: trade the opposite direction (breakout reading)",
        run_variant(df, sig_f.assign(dir=-sig_f["dir"]), sl=SL, tp=TP, ex=base_ex),
    )

    # --- ATR-scaled exits, chosen on 2009-2019 only --------------------------------
    grid = []
    for k in (0.75, 1.0, 1.5, 2.0, 3.0):
        for m in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
            t = run_variant(
                df,
                sig_f,
                sl=lambda s, k=k: k * s["atr"].to_numpy(),
                tp=lambda s, m=m: m * s["atr"].to_numpy(),
                ex=base_ex,
            )
            ins, oos = t[t["ts"] < IS_END], t[t["ts"] >= IS_END]
            grid.append(
                {
                    "sl_atr": k,
                    "tp_atr": m,
                    "is_exp_r": _exp(ins),
                    "oos_exp_r": _exp(oos),
                    "is_n": len(ins),
                    "oos_n": len(oos),
                    "_t": t,
                }
            )
    best = max(grid, key=lambda g: g["is_exp_r"])
    add(
        "atr_exits_is_best",
        f"SL {best['sl_atr']}xATR / TP {best['tp_atr']}xATR (ATR14 on 3M), picked on 2009-2019",
        best["_t"],
    )

    # --- trend / regime filters (also judged IS vs OOS) ------------------------------
    price = df["close"].to_numpy()
    with_trend = lambda s: pd.Series(  # noqa: E731
        np.sign(price[s["i"]] - s["ema_slow"]) == s["dir"], index=s.index
    )
    add(
        "with_trend_ema200",
        "Only take reversals in the direction of 3M EMA200 (pullback entry)",
        run_variant(
            df, sig_f, sl=SL, tp=TP, ex=base_ex, mask=with_trend(sig_f).reindex(sig_f.index)
        ),
    )
    add(
        "counter_trend_ema200",
        "Only take reversals against 3M EMA200 (pure fade)",
        run_variant(
            df, sig_f, sl=SL, tp=TP, ex=base_ex, mask=(~with_trend(sig_f)).reindex(sig_f.index)
        ),
    )
    # stops that scale with volatility: the default $4 is 0.44% of price in 2009, 0.1% now
    lo_vol = sig_f["atr"] < sig_f["atr"].rolling(20_000, min_periods=500).median()
    add(
        "low_vol_only",
        "Only when 3M ATR is below its trailing median (quiet tape)",
        run_variant(df, sig_f, sl=SL, tp=TP, ex=base_ex, mask=lo_vol),
    )

    # --- spread sensitivity on baseline --------------------------------------------
    spreads = []
    for sp in (0.0, 0.1, 0.2, 0.3, 0.4, 0.6):
        t = run_variant(df, sig_f, sl=SL, tp=TP, ex=ExitParams(spread=sp))
        spreads.append({"spread": sp, "exp_r": _exp(t), "net_usd": round(float(t["pnl"].sum()), 2)})

    result = {
        "generated": pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "data": {
            "from": str(df["ts"].iloc[0])[:16],
            "to": str(df["ts"].iloc[-1])[:16],
            "minutes": len(df),
            "source": "HistData XAUUSD M1 bid (NY local time)",
        },
        "assumptions": [
            "Entry on the close of the 1M candle whose forming 3M RSI crosses (live poll "
            "timing); one entry per 3M bucket.",
            f"Buy fills at ask = bid + ${args.spread:.2f}; exits on the closing side; no "
            "commission, no latency slippage.",
            "Every signal is an independent position (hedging account, no cap, no flip).",
            "SL/TP from fill; a 1M bar touching both counts as SL (pessimistic) unless noted.",
            "$ figures are per 0.01 lot (1 oz). R = P&L / stop distance.",
            "In-sample 2009-2019, out-of-sample 2020 onward.",
        ],
        "variants": [],
        "atr_grid": [{k: v for k, v in g.items() if k != "_t"} for g in grid],
        "spreads": spreads,
        "trades": _trade_rows(base.tail(400)),
    }
    for key, desc, t in variants:
        result["variants"].append(
            {"key": key, "desc": desc, "summary": summary(t), **breakdowns(t, atr_year)}
        )
    OUT.mkdir(exist_ok=True)
    result = _clean(result)
    payload = json.dumps(result, allow_nan=False)
    (OUT / "results.json").write_text(payload)
    print(f"wrote {OUT / 'results.json'} ({_time.time() - t0:.0f}s)")
    render(args.open)


def render(open_it: bool = False, stem: str = "") -> None:
    """results/{stem}results.json -> results/{stem}report.html."""
    payload = (OUT / f"{stem}results.json").read_text()
    tpl = (HERE / "ui" / "report.template.html").read_text()
    html = tpl.replace("/*__RESULTS__*/null", payload.replace("</", "<\\/"))
    out = OUT / f"{stem}report.html"
    out.write_text(html)
    print(f"wrote {out}")
    if open_it:
        subprocess.run(["open", str(out)], check=False)


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    return o


def _trade_rows(t: pd.DataFrame) -> list[dict]:
    return [
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
            "rsi": round(r.rsi, 1),
        }
        for r in t.itertuples()
    ]


if __name__ == "__main__":
    main()
