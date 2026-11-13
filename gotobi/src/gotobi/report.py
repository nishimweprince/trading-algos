"""Run the PREREG.md backtest and write results/RESULTS.md plus charts.

    uv run gotobi                          # data from $GOTOBI_DATA_DIR or the default
    uv run gotobi --data-dir /path/USDJPY --out results
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from . import stats  # noqa: E402
from .data import DEFAULT_DATA_DIR, load_m1, price_grid  # noqa: E402
from .jpcal import classify_days  # noqa: E402
from .strategy import GC_WINDOWS, day_table, event_paths  # noqa: E402

BASE_COST = 0.5
COSTS = (0.3, 0.5, 1.0, 2.0)
PERIODS = {
    "replication_2020": (date(2020, 1, 2), date(2020, 12, 31)),
    "oos_2021_2026": (date(2021, 1, 1), date(2026, 9, 30)),
}
STRATS = ["h1_plain", "h1_gc3", "h2", "combined"]
LABEL = {"h1_plain": "H1", "h1_gc3": "H1-GC3", "h2": "H2", "combined": "COMBO"}


def _fmt(v) -> str:
    if isinstance(v, float | np.floating):
        return "—" if np.isnan(v) else f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}"
    return str(v)


def _md(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join([df.index.name or ""] + cols) + " |",
             "|" + "---|" * (len(cols) + 1)]
    for idx, row in df.iterrows():
        lines.append("| " + " | ".join([str(idx)] + [_fmt(v) for v in row]) + " |")
    return "\n".join(lines)


def strategy_table(tbl: pd.DataFrame, cost: float) -> pd.DataFrame:
    rows = {}
    for s in STRATS + [f"h1_gc{n}" for n in GC_WINDOWS if n != 3]:
        for grp, mask in (("gotobi", tbl.gotobi), ("control", ~tbl.gotobi)):
            legs = tbl["combined_legs"] if s == "combined" else None
            r = stats.summarize(tbl.loc[mask, s], cost, legs)
            rows[f"{LABEL.get(s, s.upper())} {grp}"] = r
    df = pd.DataFrame(rows).T[["N", "mean", "t", "PF", "PR", "W", "total"]]
    df.index.name = f"net @ {cost} pip"
    return df


def evaluate(tbl: pd.DataFrame) -> tuple[dict, str]:
    g, c = tbl[tbl.gotobi], tbl[~tbl.gotobi]
    h1 = stats.summarize(g.h1_plain, BASE_COST)
    h1_diff = stats.welch(g.h1_plain, c.h1_plain)
    h1_cost1 = stats.summarize(g.h1_plain, 1.0)
    t1 = h1["p_one_sided"] < 0.05 and h1["mean"] > 0 and h1_diff["p_one_sided"] < 0.05
    sec_p = {
        "H1-GC3 net": stats.summarize(g.h1_gc3, BASE_COST)["p_one_sided"],
        "H2 net": stats.summarize(g.h2, BASE_COST)["p_one_sided"],
        "COMBO net": stats.summarize(g.combined, BASE_COST, g.combined_legs)["p_one_sided"],
        "H2 gotobi-control": stats.welch(g.h2, c.h2)["p_one_sided"],
    }
    sec = stats.holm(sec_p)
    if t1 and h1_cost1["mean"] > 0:
        verdict = "PASS"
    elif any(sec.values()):
        verdict = "SECONDARY ONLY"
    else:
        verdict = "KILL"
    detail = {
        "T1 H1 net": h1, "T1 H1 gotobi-control (gross)": h1_diff, "H1 net @1.0": h1_cost1,
        "T1 pass": t1, "secondary p": sec_p, "secondary Holm reject": sec,
    }
    return detail, verdict


def charts(grid: pd.Series, days: dict, tbl: pd.DataFrame, out: Path, tag: str) -> list[str]:
    files = []
    gd = {d: v for d, v in days.items() if v}
    cd = {d: v for d, v in days.items() if not v}
    pg, pc = event_paths(grid, gd), event_paths(grid, cd)
    x = np.arange(len(pg.index))
    ticks = [i for i, s in enumerate(pg.index) if s.endswith(":00")]

    fig, ax = plt.subplots(1, 2, figsize=(13, 4.2))
    ax[0].plot(x, pg.mean(axis=1), label=f"Gotobi (n={pg.shape[1]})", color="#c0392b")
    ax[0].plot(x, pc.mean(axis=1), label=f"control (n={pc.shape[1]})", color="#7f8c8d")
    ax[0].axvline(pg.index.get_loc("09:55"), ls=":", color="k")
    ax[0].axhline(0, lw=0.5, color="k")
    ax[0].set_title(f"Mean price minus p(09:55), pips — {tag}")
    ax[1].plot(x, (pg > 0).sum(axis=1) / pg.notna().sum(axis=1), color="#c0392b", label="Gotobi")
    ax[1].plot(x, (pc > 0).sum(axis=1) / pc.notna().sum(axis=1), color="#7f8c8d", label="control")
    ax[1].axvline(pg.index.get_loc("09:55"), ls=":", color="k")
    ax[1].set_title("P(price > p(09:55))")
    for a in ax:
        a.set_xticks(ticks, [pg.index[i] for i in ticks], rotation=45)
        a.set_xlabel("JST")
        a.legend()
    fig.tight_layout()
    f = out / f"event_{tag}.png"
    fig.savefig(f, dpi=110)
    plt.close(fig)
    files.append(f.name)

    fig, ax = plt.subplots(figsize=(10, 4.2))
    for s, col in zip(STRATS, ["#c0392b", "#e67e22", "#2980b9", "#27ae60"], strict=True):
        legs = tbl["combined_legs"] if s == "combined" else 1
        net = (tbl.loc[tbl.gotobi, s] - BASE_COST * (legs[tbl.gotobi] if s == "combined" else 1))
        net = net.dropna()
        ax.plot(pd.to_datetime(net.index), net.cumsum(), label=f"{LABEL[s]} Gotobi", color=col)
    ctrl = (tbl.loc[~tbl.gotobi, "h1_plain"] - BASE_COST).dropna()
    ax.plot(pd.to_datetime(ctrl.index), ctrl.cumsum(), label="H1 control", color="#7f8c8d", ls="--")
    ax.axhline(0, lw=0.5, color="k")
    ax.set_title(f"Cumulative net pips @ {BASE_COST} pip/round trip — {tag}")
    ax.legend()
    fig.tight_layout()
    f = out / f"equity_{tag}.png"
    fig.savefig(f, dpi=110)
    plt.close(fig)
    files.append(f.name)
    return files


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--out", default="results")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    bars = load_m1(a.data_dir)
    grid = price_grid(bars)
    md = ["# Gotobi backtest results", "",
          "Generated by `uv run gotobi`; rules and gates in [PREREG.md](../PREREG.md).",
          f"Data: {len(bars):,} M1 bars, {bars.index[0]} → {bars.index[-1]} (JST).", ""]
    for name, (start, end) in PERIODS.items():
        days = classify_days(start, end)
        tbl = day_table(grid, days)
        tbl.to_csv(out / f"days_{name}.csv")
        md += [f"## {name}", "",
               f"{int(tbl.gotobi.sum())} Gotobi days, {int((~tbl.gotobi).sum())} control days.", ""]
        md += [_md(strategy_table(tbl, BASE_COST)), ""]
        sens = {}
        for cst in COSTS:
            for s in STRATS:
                legs = tbl.loc[tbl.gotobi, "combined_legs"] if s == "combined" else None
                sens[(LABEL[s], cst)] = stats.summarize(tbl.loc[tbl.gotobi, s], cst, legs)["mean"]
        sdf = pd.Series(sens).unstack()
        sdf.index.name = "Gotobi net mean pips by cost"
        md += [_md(sdf), ""]
        post = stats.welch(tbl.loc[tbl.gotobi, "post_fix_1m"], tbl.loc[~tbl.gotobi, "post_fix_1m"])
        md += [f"09:55→09:56 move, Gotobi − control: {post['diff']:.3f} pips "
               f"(t = {post['t']:.2f}).", ""]
        for s in ("h1_plain", "h2"):
            yt = stats.by_year(tbl.loc[tbl.gotobi, s], BASE_COST)
            yt.index.name = f"{LABEL[s]} Gotobi by year"
            md += [_md(yt), ""]
        lo, hi = stats.bootstrap_mean_ci(tbl.loc[tbl.gotobi, "h1_plain"] - BASE_COST)
        md += [f"H1 Gotobi net mean, block-bootstrap 95% CI: [{lo:.2f}, {hi:.2f}] pips.", ""]
        if name.startswith("oos"):
            detail, verdict = evaluate(tbl)
            md += ["### Pre-registered verdict", "", f"**{verdict}**", "", "```"]
            for k, v in detail.items():
                if isinstance(v, dict):
                    v = {kk: (round(float(vv), 4) if isinstance(vv, float | np.floating)
                              else vv)
                         for kk, vv in v.items()}
                md.append(f"{k}: {v}")
            md += ["```", ""]
        for f in charts(grid, days, tbl, out, name):
            md += [f"![{f}]({f})", ""]
    (out / "RESULTS.md").write_text("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
