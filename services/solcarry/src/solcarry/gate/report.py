"""Markdown report, PNG charts and the ui.json bundle consumed by the dashboard."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .. import EVIDENCE_TIER  # noqa: E402
from ..analytics import depth as depth_mod  # noqa: E402
from ..config import Config, Paths  # noqa: E402

CAVEATS = [
    "Native-stake yield is approximated as y_lst / (1 - pool_fee) (Gate 1 only).",
    "D2-MANAGED is OPTIMISTIC: it assumes margin top-ups always arrive within T_transfer. "
    "Gate 2 replaces it with a path simulation.",
    "Hurdle uses DTB3, a discount-basis T-bill yield, converted as ln(1 + DTB3/100): an approximation.",
    "Velocity history starts at the mid-2026 relaunch; Velocity-based results are SHORT_SAMPLE.",
    "Binance SOLUSDT funding is a PROXY for funding-regime behaviour, not the venue that would be traded.",
    "Hyperliquid history starts 2023-05; its 8-hourly first weeks are dropped (ambiguous period). "
    "HL margin is USDC only here: LST-as-collateral is not modelled.",
    "Gated (*-G) variants: the rule is pre-registered in `gating` (part of the lock); OFF assumes both "
    "legs close at the switch hour and the cash earns the T-bill rate.",
    "Reserve sizing is a single-margin-call closed form; repeated calls, funding paid during stress and "
    "correlated LST depeg are Gate 2.",
    "Venue/counterparty, smart-contract and custody risk are not modelled; they remain qualitative gates.",
    "Staking history (DeFiLlama) starts 2025-03-26; windows involving staking are clipped to that start.",
]

PALETTE = ["#2f6fdb", "#e0782f", "#2a9d6f", "#c2477a", "#7a5cd6", "#8a8f2a", "#3b9fb8"]


def _pct(x: Any, d: int = 2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return f"{float(x) * 100:.{d}f}%"


def _num(x: Any, d: int = 2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    if isinstance(x, float) and math.isinf(x):
        return "∞"
    return f"{float(x):,.{d}f}"


def md_table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "_no data_\n"
    head = "| " + " | ".join(h for _, h in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = ["| " + " | ".join(str(r.get(k, "")) for k, _ in cols) + " |" for r in rows]
    return "\n".join([head, sep, *body]) + "\n"


def _read_csv(p: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(p) if p.exists() and p.stat().st_size > 1 else pd.DataFrame()
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _read_pq(p: Path) -> pd.DataFrame:
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


# ------------------------------------------------------------------ charts
def _save(fig: Any, path: Path) -> str:
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path.name


def charts(run_dir: Path, summ: dict[str, Any]) -> list[str]:
    out: list[str] = []
    fd = _read_pq(run_dir / "funding_daily.parquet")
    if not fd.empty:
        fig, ax = plt.subplots(figsize=(10, 4))
        for col, lab, c in (
            ("binance_7d", "Binance SOLUSDT (proxy)", PALETTE[0]),
            ("velocity_7d", "Velocity SOL-PERP", PALETTE[1]),
            ("hyperliquid_7d", "Hyperliquid SOL", PALETTE[2]),
        ):
            if col in fd and fd[col].notna().any():
                ax.plot(fd.index, fd[col] * 100, label=lab, color=c, lw=1.2)
        ax.axhline(0, color="#888", lw=0.8)
        ax.set_ylabel("%/yr (7d rolling, short receives +)")
        ax.set_title("Funding, annualized")
        ax.legend()
        out.append(_save(fig, run_dir / "chart_funding.png"))

    comps = {v: s.get("components") for v, s in summ["summaries"].items() if s.get("kind") == "P"}
    if comps:
        fig, ax = plt.subplots(figsize=(8, 3.5))
        names = list(comps)
        y = np.array([comps[n]["y"] or 0 for n in names]) * 100
        f = np.array([comps[n]["funding"] or 0 for n in names]) * 100
        cst = np.array([comps[n]["cost"] or 0 for n in names]) * 100
        ax.barh(names, y, color=PALETTE[2], label="staking y")
        ax.barh(names, f, left=np.where(f >= 0, y, 0), color=PALETTE[0], label="funding")
        ax.barh(names, cst, left=np.where(f < 0, f, 0), color=PALETTE[3], label="costs")
        ax.axvline(0, color="#888", lw=0.8)
        ax.set_xlabel("%/yr on long notional (before dividing by k_P)")
        ax.set_title("Variant P carry components (sample means)")
        ax.legend(loc="lower right", fontsize=8)
        out.append(_save(fig, run_dir / "chart_perp_components.png"))

    pw = _read_pq(run_dir / "perp_windows.parquet")
    cyc = _read_csv(run_dir / "dated_cycles.csv")
    if not pw.empty or not cyc.empty:
        fig, ax = plt.subplots(figsize=(10, 4))
        if not pw.empty:
            w = pw[pw["window_days"] == 30]
            for i, (v, g) in enumerate(w.groupby("variant")):
                ax.hist(
                    g["X_W"] * 100, bins=40, alpha=0.45, label=f"{v} (30d)", color=PALETTE[i % 7]
                )
        if not cyc.empty:
            sc = cyc[cyc["scored"]]
            for i, (v, g) in enumerate(sc.groupby("variant")):
                ax.scatter(
                    g["X"] * 100,
                    np.full(len(g), -1 - i),
                    marker="|",
                    s=200,
                    color=PALETTE[(i + 4) % 7],
                    label=f"{v} cycles",
                )
        ax.axvline(0, color="#888", lw=0.8)
        ax.set_xlabel("excess over hurdle, %/yr")
        ax.set_title("Excess carry distribution")
        ax.legend(fontsize=8)
        out.append(_save(fig, run_dir / "chart_excess.png"))

    ent = _read_pq(run_dir / "dated_entries.parquet")
    if not ent.empty:
        fig, ax = plt.subplots(figsize=(10, 4))
        for i, (sym, g) in enumerate(ent.groupby("symbol")):
            ax.plot(g["dte_days"], g["a"] * 100, lw=0.8, label=sym, color=PALETTE[i % 7])
        ax.invert_xaxis()
        ax.axhline(0, color="#888", lw=0.8)
        ax.set_xlabel("days to expiry")
        ax.set_ylabel("annualized basis %/yr")
        ax.set_title("COIN-M quarterly basis vs dte")
        ax.legend(fontsize=7, ncol=2)
        out.append(_save(fig, run_dir / "chart_basis.png"))

    cdf = summ.get("runup_cdf", {})
    if cdf:
        fig, ax = plt.subplots(figsize=(8, 4))
        grid = np.linspace(0, 1, 201)
        for i, (T, vals) in enumerate(cdf.items()):
            ax.plot(np.array(vals) * 100, grid, label=f"T={T}h", color=PALETTE[i % 7])
        ax.set_xscale("symlog", linthresh=1)
        ax.set_xlabel("run-up U_T, % (symlog)")
        ax.set_ylabel("CDF")
        ax.set_title("SOL run-up distributions")
        ax.legend()
        out.append(_save(fig, run_dir / "chart_runups.png"))
    return out


# ------------------------------------------------------------------ report
def build_report(cfg: Config, paths: Paths, run_dir: Path) -> Path:
    summ = json.loads((run_dir / "summaries.json").read_text())
    gate = (
        json.loads((run_dir / "gate.json").read_text())
        if (run_dir / "gate.json").exists()
        else None
    )
    val = (
        json.loads((run_dir / "validation.json").read_text())
        if (run_dir / "validation.json").exists()
        else None
    )
    imgs = charts(run_dir, summ)

    hist = depth_mod.load_history(paths.results)
    if not hist.empty:
        last_ts = hist["ts"].max()
        depth_now = hist[hist["ts"] == last_ts].copy()
        depth_now.to_csv(run_dir / "depth.csv", index=False)
    else:
        depth_now = pd.DataFrame()

    L: list[str] = []
    L.append(f"# solcarry Gate 1 report — run `{run_dir.name}`\n")
    L.append(
        f"**EVIDENCE TIER: {EVIDENCE_TIER}** — a PASS means *proceed to paper trading*, never *go live*.\n"
    )
    if gate is None:
        L.append("> Gate not run yet (`solcarry gate`).\n")
    elif gate["unlocked"]:
        L.append(
            f"> **UNLOCKED: thresholds not pre-registered** ({gate['lock']['state']}: {gate['lock']['detail']})\n"
        )
    else:
        L.append(f"Lock: LOCKED at {gate['lock']['locked_at']} ({gate['lock']['detail']})\n")

    L.append("\n## Data coverage\n")
    cov_rows = []
    for k, v in summ["coverage"].items():
        if k == "quarterly":
            cov_rows.append(
                {"src": k, "rows": v["rows"], "start": ", ".join(v["contracts"]), "end": ""}
            )
        else:
            cov_rows.append(
                {"src": k, "rows": v["rows"], "start": v["start"] or "", "end": v["end"] or ""}
            )
    L.append(
        md_table(
            cov_rows,
            [("src", "table"), ("rows", "rows"), ("start", "start / contracts"), ("end", "end")],
        )
    )

    L.append("\n## Validation\n")
    if val:
        L.append(f"Overall: **{'OK' if val['ok'] else 'FAILED'}**\n\n")
        L.append(
            md_table(
                [
                    {
                        "n": c["name"],
                        "l": c["level"],
                        "ok": "✅" if c["ok"] else ("❌" if c["level"] == "hard" else "⚠️"),
                        "d": c["detail"],
                    }
                    for c in val["checks"]
                ],
                [("n", "check"), ("l", "level"), ("ok", "ok"), ("d", "detail")],
            )
        )
    else:
        L.append("_not run_\n")

    L.append("\n## 1. Gate result\n")
    if gate:
        L.append(f"### {gate['result']}\n")
        imp = gate.get("depth_impact")
        L.append(
            f"Scored at α = {summ['gate_alpha']}. Depth criterion uses the latest "
            f"{gate['depth_target']['lst']} impact at {gate['depth_target']['size']:g} tokens: "
            f"{_pct(imp, 3)} (snapshot {gate.get('depth_impact_ts') or 'none'}).\n\n"
        )
        rows = []
        for s in gate["scores"]:
            c = {x["key"]: x for x in s["criteria"]}

            def cell(k: str, pct: bool = True, c: dict[str, Any] = c) -> str:
                if k not in c:
                    return "—"
                x = c[k]
                val_s = _pct(x["value"]) if pct else _num(x["value"], 0)
                mark = "" if x["ok"] is None else (" ✅" if x["ok"] else " ❌")
                return val_s + mark

            rows.append(
                {
                    "v": s["variant"],
                    "st": f"**{s['status']}**",
                    "med": cell("excess_median"),
                    "p5": cell("excess_p5"),
                    "neg": cell("share_negative"),
                    "run": cell("neg_run_days", False),
                    "dep": cell("depth_impact"),
                    "days": cell("sample_days", False),
                    "fl": ", ".join(s["flags"]),
                    "note": s["note"],
                }
            )
        L.append(
            md_table(
                rows,
                [
                    ("v", "variant"),
                    ("st", "status"),
                    ("med", "median X"),
                    ("p5", "p5 X"),
                    ("neg", "share X<0"),
                    ("run", "neg run d"),
                    ("dep", "depth"),
                    ("days", "sample d"),
                    ("fl", "flags"),
                    ("note", "note"),
                ],
            )
        )
        th = cfg.thresholds
        L.append(
            f"\nBars: median > {_pct(th.excess_median_min)}, p5 > {_pct(th.excess_p5_min)}, "
            f"share negative < {_pct(th.share_negative_max, 0)}, neg run < {th.neg_run_max_days:g} d (P), "
            f"depth impact > {_pct(th.depth_impact_max)} (LST variants), sample ≥ {th.min_sample_days:g} d.\n"
        )

    L.append("\n## 2. Funding diagnostics\n")
    L.append(
        md_table(
            [
                {
                    "s": d["source"],
                    "h": d.get("hours", 0),
                    "span": f"{(d.get('start') or '')[:10]} → {(d.get('end') or '')[:10]}",
                    "pos": _pct(d.get("share_positive"), 1),
                    "med": _pct(d.get("median_annual")),
                    "mean": _pct(d.get("mean_annual")),
                    "p5": _pct(d.get("p5_annual")),
                    "p95": _pct(d.get("p95_annual")),
                    "nr": _num(d.get("longest_negative_run_days_7d"), 1),
                    "fl": _pct(d.get("share_at_floor"), 1),
                }
                for d in summ["funding_diagnostics"]
            ],
            [
                ("s", "source"),
                ("h", "hours"),
                ("span", "span"),
                ("pos", "share +"),
                ("med", "median/yr"),
                ("mean", "mean/yr"),
                ("p5", "p5"),
                ("p95", "p95"),
                ("nr", "longest neg run d (7d mean)"),
                ("fl", "at floor"),
            ],
        )
    )
    if "chart_funding.png" in imgs:
        L.append("\n![funding](chart_funding.png)\n")

    L.append("\n## 3. Run-ups and reserves\n")
    rt = _read_csv(run_dir / "runups.csv")
    if not rt.empty:
        cols = [c for c in rt.columns if c.startswith("q")] + ["max"]
        L.append(
            md_table(
                [
                    {"T": int(r["window_h"]), "n": int(r["n"]), **{c: _pct(r[c]) for c in cols}}
                    for _, r in rt.iterrows()
                ],
                [("T", "T (h)"), ("n", "n")] + [(c, c) for c in cols],
            )
        )
    res = summ["reserves"]
    rrows = [
        {
            "v": k,
            "T": v["T_h"],
            "q": _pct(v["q_runup"]),
            "b": _pct(v["beta_P"]),
            "k": _num(v["k_P"], 3),
        }
        for k, v in res.items()
        if isinstance(v, dict) and "beta_P" in v
    ]
    if rrows:
        L.append(
            f"\nPerp reserve at α = {res['alpha']}: β_P = (1 + mm)·q_α(U_T), k_P = 1 + im + β_P\n\n"
        )
        L.append(
            md_table(
                rrows,
                [("v", "variant"), ("T", "T (h)"), ("q", "q_α(U_T)"), ("b", "β_P"), ("k", "k_P")],
            )
        )
    if "coinm" in res:
        cm = res["coinm"]
        L.append(
            f"\nInverse margin φ(U) = (U + mm)/(1 + U), mm = {cm['mm']}: "
            f"q_α(U_hold) = {_pct(cm['q_U_hold'])} → φ_static = {_pct(cm['phi']['D2-STATIC'])}; "
            f"q_α(U_transfer) = {_pct(cm['q_U_transfer'])} → φ_managed = {_pct(cm['phi']['D2-MANAGED'])}. "
            "A short fully collateralized in SOL cannot be liquidated (φ < 1).\n"
        )
    if "chart_runups.png" in imgs:
        L.append("\n![runups](chart_runups.png)\n")

    L.append("\n## 4. Variant P (perp carry)\n")
    pw = _read_pq(run_dir / "perp_windows.parquet")
    if not pw.empty:
        rows = []
        for (v, D), g in pw.groupby(["variant", "window_days"]):
            x = g["X_W"]
            rows.append(
                {
                    "v": v,
                    "D": int(str(D)),
                    "n": len(x),
                    "med": _pct(x.median()),
                    "p5": _pct(x.quantile(0.05)),
                    "p95": _pct(x.quantile(0.95)),
                    "neg": _pct((x < 0).mean(), 1),
                    "R": _pct(g["R_W"].median()),
                }
            )
        L.append(
            md_table(
                rows,
                [
                    ("v", "variant"),
                    ("D", "window d"),
                    ("n", "windows"),
                    ("med", "median X"),
                    ("p5", "p5 X"),
                    ("p95", "p95 X"),
                    ("neg", "share X<0"),
                    ("R", "median R"),
                ],
            )
        )
        rg = _read_csv(run_dir / "perp_regime.csv")
        if not rg.empty:
            L.append("\nRegime split (30-day X_W by tercile of trailing 30-day SOL return):\n\n")
            L.append(
                md_table(
                    [
                        {
                            "v": r["variant"],
                            "t": r["tercile"],
                            "r": f"{_pct(r['ret_lo'], 0)} … {_pct(r['ret_hi'], 0)}",
                            "n": int(r["n"]),
                            "m": _pct(r["X_median"]),
                            "neg": _pct(r["share_neg"], 0),
                        }
                        for _, r in rg.iterrows()
                    ],
                    [
                        ("v", "variant"),
                        ("t", "tercile"),
                        ("r", "SOL 30d return"),
                        ("n", "n"),
                        ("m", "median X"),
                        ("neg", "share X<0"),
                    ],
                )
            )
        gt = _read_csv(run_dir / "perp_gating.csv")
        if not gt.empty:
            gc = cfg.gating
            L.append(
                f"\nFunding gate (*-G): hedge ON while trailing {gc.lookback_days:g}-day funding + "
                f"staking − resize − r_f > +{_pct(gc.hysteresis_annual, 1)}, OFF below "
                f"−{_pct(gc.hysteresis_annual, 1)}; OFF = cash at r_f.\n\n"
            )
            L.append(
                md_table(
                    [
                        {
                            "v": r["variant"],
                            "on": _pct(r["on_share"], 1),
                            "n": int(r["switches"]),
                            "py": _num(r["switches_per_year"], 1),
                            "d": _pct(r["switch_drag"]),
                        }
                        for _, r in gt.iterrows()
                    ],
                    [
                        ("v", "variant"),
                        ("on", "time ON"),
                        ("n", "switches"),
                        ("py", "per year"),
                        ("d", "switch drag /yr"),
                    ],
                )
            )
    else:
        L.append("_no P windows (insufficient overlapping data)_\n")
    for img, cap in (("chart_perp_components.png", "components"), ("chart_excess.png", "excess")):
        if img in imgs:
            L.append(f"\n![{cap}]({img})\n")

    L.append("\n## 5. Variant D (dated basis carry)\n")
    cyc = _read_csv(run_dir / "dated_cycles.csv")
    if not cyc.empty:
        rows = [
            {
                "v": r["variant"],
                "c": r["contract"],
                "e": str(r["entry_ts"])[:10],
                "dte": _num(r["dte_entry"], 1),
                "b": _pct(r["basis_cycle"], 3),
                "a": _pct(r["a_cycle"]),
                "st": _pct(r["staking_contrib"]),
                "cost": _pct(r["cost"]),
                "R": _pct(r["R"]),
                "X": _pct(r["X"]),
                "s": r["status"] + ("" if r["scored"] else " (unscored)"),
            }
            for _, r in cyc.iterrows()
        ]
        L.append(
            md_table(
                rows,
                [
                    ("v", "variant"),
                    ("c", "contract"),
                    ("e", "entry"),
                    ("dte", "dte"),
                    ("b", "basis"),
                    ("a", "annualized"),
                    ("st", "staking"),
                    ("cost", "costs"),
                    ("R", "net R"),
                    ("X", "excess X"),
                    ("s", "status"),
                ],
            )
        )
    eq = _read_csv(run_dir / "dated_entry_quantiles.csv")
    if not eq.empty:
        qc = [c for c in eq.columns if c.startswith("q")]
        L.append(
            f"\nEntry distribution of annualized basis a_t (dte ∈ [{cfg.dated.dte_min:g}, {cfg.dated.dte_max:g}]):\n\n"
        )
        L.append(
            md_table(
                [
                    {"s": r["symbol"], "n": int(r["n"]), **{c: _pct(r[c]) for c in qc}}
                    for _, r in eq.iterrows()
                ],
                [("s", "contract"), ("n", "hours")] + [(c, c) for c in qc],
            )
        )
    if "chart_basis.png" in imgs:
        L.append("\n![basis](chart_basis.png)\n")

    L.append(
        f"\n## 6. Capital sizing (target ${cfg.sizing.target_per_quarter_usd:,.0f}/quarter, reconstructed)\n"
    )
    sz = _read_csv(run_dir / "sizing.csv")
    if not sz.empty:
        L.append(
            md_table(
                [
                    {
                        "v": r["variant"],
                        "rm": _pct(r["R_median"]),
                        "rp": _pct(r["R_p25"]),
                        "cm": "∞"
                        if not np.isfinite(r["capital_at_median"])
                        else f"${r['capital_at_median']:,.0f}",
                        "cp": "∞"
                        if not np.isfinite(r["capital_at_p25"])
                        else f"${r['capital_at_p25']:,.0f}",
                    }
                    for _, r in sz.iterrows()
                ],
                [
                    ("v", "variant"),
                    ("rm", "median R"),
                    ("rp", "p25 R"),
                    ("cm", "capital @ median"),
                    ("cp", "capital @ p25"),
                ],
            )
        )

    L.append("\n## 7. LST exit depth (Jupiter, point-in-time)\n")
    if not depth_now.empty:
        L.append(
            f"Snapshot {pd.Timestamp(depth_now['ts'].iloc[0]).isoformat()}; history rows: {len(hist)}.\n\n"
        )
        L.append(
            md_table(
                [
                    {
                        "l": r["lst"],
                        "q": _num(r["size"], 0),
                        "o": _num(r["out_sol"], 3),
                        "px": _num(r["px"], 5),
                        "i": _pct(r["impact"], 3),
                        "j": _num(r["jup_price_impact_pct"], 5),
                        "r": r["routes"],
                    }
                    for _, r in depth_now.iterrows()
                ],
                [
                    ("l", "LST"),
                    ("q", "size"),
                    ("o", "out SOL"),
                    ("px", "SOL/LST"),
                    ("i", "impact vs 1"),
                    ("j", "jup priceImpactPct"),
                    ("r", "routes"),
                ],
            )
        )
    else:
        L.append("_no depth snapshot; run `solcarry depth`_\n")

    L.append("\n## 8. Caveats\n")
    L.extend(f"- {c}\n" for c in CAVEATS)

    rp = run_dir / "report.md"
    rp.write_text("".join(x if x.endswith("\n") else x + "\n" for x in L))
    build_ui_bundle(run_dir, summ, gate, val, depth_now, hist)
    return rp


# ------------------------------------------------------------------ ui bundle
def _series(df: pd.DataFrame, cols: list[str]) -> dict[str, Any]:
    if df.empty:
        return {"t": [], **{c: [] for c in cols}}
    d = df[[c for c in cols if c in df.columns]]
    return {
        "t": [ts.strftime("%Y-%m-%d") for ts in pd.DatetimeIndex(d.index)],
        **{c: [None if pd.isna(x) else float(x) for x in d[c]] for c in d.columns},
    }


def build_ui_bundle(
    run_dir: Path,
    summ: dict[str, Any],
    gate: dict[str, Any] | None,
    val: dict[str, Any] | None,
    depth_now: pd.DataFrame,
    hist: pd.DataFrame,
) -> None:
    from ..pipeline import write_json

    fd = _read_pq(run_dir / "funding_daily.parquet")
    pw = _read_pq(run_dir / "perp_windows.parquet")
    xw: dict[str, Any] = {}
    if not pw.empty:
        for v, g in pw[pw["window_days"] == 30].groupby("variant"):
            g = g.sort_values("end")
            xw[str(v)] = {
                "t": [pd.Timestamp(x).strftime("%Y-%m-%d") for x in g["end"]],
                "x": [float(x) for x in g["X_W"]],
            }
    ent = _read_pq(run_dir / "dated_entries.parquet")
    basis: dict[str, Any] = {}
    if not ent.empty:
        for sym, g in ent.groupby("symbol"):
            g = g.iloc[::6]
            basis[str(sym)] = {
                "dte": [round(float(x), 2) for x in g["dte_days"]],
                "a": [float(x) for x in g["a"]],
            }
    cyc = _read_csv(run_dir / "dated_cycles.csv")
    depth_hist = []
    if not hist.empty:
        h = hist.dropna(subset=["impact"])
        for (lst, size), g in h.groupby(["lst", "size"]):
            if float(str(size)) <= 1:
                continue
            depth_hist.append(
                {
                    "lst": lst,
                    "size": float(str(size)),
                    "t": [pd.Timestamp(x).isoformat() for x in g["ts"]],
                    "impact": [float(x) for x in g["impact"]],
                }
            )
    bundle = {
        "run_id": run_dir.name,
        "evidence_tier": EVIDENCE_TIER,
        "summary": summ,
        "gate": gate,
        "validation": val,
        "funding_daily": _series(
            fd,
            ["binance_7d", "velocity_7d", "hyperliquid_7d", "binance", "velocity", "hyperliquid"],
        ),
        "gating": _read_csv(run_dir / "perp_gating.csv")
        .replace({np.inf: None, np.nan: None})
        .to_dict("records"),
        "xw30": xw,
        "basis": basis,
        "cycles": cyc.replace({np.nan: None}).to_dict("records") if not cyc.empty else [],
        "runups": _read_csv(run_dir / "runups.csv").to_dict("records"),
        "regime": _read_csv(run_dir / "perp_regime.csv").replace({np.nan: None}).to_dict("records"),
        "sizing": _read_csv(run_dir / "sizing.csv")
        .replace({np.inf: None, np.nan: None})
        .to_dict("records"),
        "depth": depth_now.assign(ts=depth_now["ts"].astype(str)).to_dict("records")
        if not depth_now.empty
        else [],
        "depth_history": depth_hist,
        "caveats": CAVEATS,
    }
    write_json(run_dir / "ui.json", bundle)
