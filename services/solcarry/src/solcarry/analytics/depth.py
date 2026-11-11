"""LST exit depth from Jupiter quotes: impact(Q) = (out(Q)/Q) / (out(1)/1) - 1."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..config import Config
from ..sources.jupiter import DECIMALS, Jupiter

HISTORY_FILE = "depth_history.parquet"


def snapshot(cfg: Config, jup: Jupiter, now: pd.Timestamp | None = None) -> pd.DataFrame:
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    rows = []
    for name, mint in cfg.depth.mints.items():
        ref = jup.quote(mint, 1.0)
        ref_px = ref.out_amount / 10**DECIMALS if ref else float("nan")
        for q in cfg.depth.sizes:
            qt = ref if (q == 1.0 and ref is not None) else jup.quote(mint, q)
            if qt is None:
                rows.append(
                    {
                        "ts": now,
                        "lst": name,
                        "size": q,
                        "out_sol": float("nan"),
                        "px": float("nan"),
                        "impact": float("nan"),
                        "jup_price_impact_pct": float("nan"),
                        "routes": "QUOTE_FAILED",
                    }
                )
                continue
            out = qt.out_amount / 10**DECIMALS
            px = out / q
            rows.append(
                {
                    "ts": now,
                    "lst": name,
                    "size": q,
                    "out_sol": out,
                    "px": px,
                    "impact": px / ref_px - 1.0 if ref_px == ref_px else float("nan"),
                    "jup_price_impact_pct": qt.price_impact_pct,
                    "routes": qt.routes,
                }
            )
    return pd.DataFrame(rows)


def append_history(df: pd.DataFrame, results_dir: Path) -> pd.DataFrame:
    p = results_dir / HISTORY_FILE
    hist = pd.read_parquet(p) if p.exists() else pd.DataFrame()
    out = pd.concat([hist, df], ignore_index=True) if not hist.empty else df
    results_dir.mkdir(parents=True, exist_ok=True)
    out.to_parquet(p)
    return out


def load_history(results_dir: Path) -> pd.DataFrame:
    p = results_dir / HISTORY_FILE
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def latest_impact(hist: pd.DataFrame, lst: str, size: float) -> tuple[float, str | None]:
    if hist.empty:
        return float("nan"), None
    h = hist[(hist["lst"] == lst) & (hist["size"] == size)].dropna(subset=["impact"])
    if h.empty:
        return float("nan"), None
    last = h.sort_values("ts").iloc[-1]
    return float(last["impact"]), pd.Timestamp(last["ts"]).isoformat()
