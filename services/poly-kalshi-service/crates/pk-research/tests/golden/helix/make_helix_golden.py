"""Provenance of the Helix goldens: the Python Helix modules (build 1286ad5) on
the synthetic tape of ``make_helix_tape.py``. Run from services/poly-kalshi-service
with the research extra:

    PYTHONPATH=. python crates/pk-research/tests/golden/helix/make_helix_golden.py

Writes next to this file:
- ``venue_cache/``: canned Gamma and Kalshi answers, so ``resolutions`` fetches nothing;
- ``resolutions.json`` (``resolutions.build`` + ``summarise``);
- ``replay_<day>.json.gz``: every ``helix_replay.run_day`` output as {columns, data},
  and its summary without timings;
- ``inventory.json``, ``validate_<day>.json``, ``leadlag.json``, ``sweeps.json``,
  ``fairvalue.json``.
"""

from __future__ import annotations

import gzip
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
TAPE = HERE / "tape"
DAYS = ["20261001", "20261002"]
sys.path.insert(0, str(HERE.parents[4]))

from research import helix_fairvalue, helix_leadlag, helix_replay, helix_sweeps  # noqa: E402
from research.helix import inventory, reader, resolutions, validate  # noqa: E402


def clean(v):
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    return v


def frame(path: Path) -> dict:
    df = pd.read_parquet(path)
    return {"columns": list(df.columns), "dtypes": [str(t) for t in df.dtypes],
            "data": clean(df.astype(object).values.tolist())}  # fmt: skip


def venue_cache(cache: Path) -> None:
    """Gamma and Kalshi as they would answer, from the tape's own windows."""
    cache.mkdir(parents=True, exist_ok=True)
    for day in DAYS:
        truth = {}
        for line in (TAPE / f"outcomes_{day}.txt").read_text().splitlines():
            a, o, v = line.split()
            truth[(a, int(o))] = v
        windows = list(reader.iter_windows(day, TAPE))
        for asset in reader.ASSETS:
            poly, kalshi = [], []
            for i, w in enumerate(x for x in windows if reader.HELIX_15M[x.asset] == asset):
                out = truth[(asset, w.open_unix)]
                if i == 2:
                    out = "no" if out == "yes" else "yes"  # Gamma disagrees with the tape here
                src = "https://data.chain.link/streams/" + asset.lower() + "-usd"
                if day == "20261002":
                    src += "-twap-60s-streams"
                poly.append({
                    "slug": f"{asset.lower()}-updown-15m-{w.open_unix}",
                    "outcomePrices": '["1", "0"]' if out == "yes" else '["0", "1"]',
                    "closed": i != 4,
                    "resolutionSource": src,
                    "feeSchedule": {"rate": 0.07, "exponent": 1},
                    "feesEnabled": True,
                    "outcomes": '["Up", "Down"]',
                })  # fmt: skip
                close = pd.Timestamp(w.open_unix + 900, unit="s", tz="UTC")
                kalshi.append({
                    "ticker": f"KX{asset}15M-{w.open_unix}",
                    "open_time": (close - pd.Timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "close_time": close.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "result": out if i != 1 else "",
                    "status": "finalized",
                    "floor_strike": w.strike,
                    "expiration_value": f"{w.strike * 1.0003:,.2f}" if i != 3 else "",
                })  # fmt: skip
            (cache / f"poly_{asset}_{day}.json").write_text(json.dumps(poly))
            (cache / f"kalshi_{asset}_{day}.json").write_text(json.dumps(kalshi))


def main() -> None:
    cache = HERE / "venue_cache"
    venue_cache(cache)
    rows = resolutions.build(DAYS, cache, TAPE)
    (HERE / "resolutions.json").write_text(
        json.dumps(clean({"rows": rows, "summary": resolutions.summarise(rows)}), indent=0)
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data = tmp / "data"
        helix_dir = data / "helix"
        helix_dir.mkdir(parents=True)
        res_path = data / "resolutions.parquet"
        pd.DataFrame(rows).to_parquet(res_path, index=False)
        res_map = helix_replay._load_resolutions(res_path)
        for day in DAYS:
            summary = helix_replay.run_day(day, helix_dir, TAPE, res_map, {})
            summary.pop("timings_s")
            summary.pop("seconds")
            out = {"summary": clean(summary)}
            for name in ("crossings", "windows", "grid", "slices"):
                out[name] = frame(helix_dir / day / f"{name}.parquet")
            with gzip.open(HERE / f"replay_{day}.json.gz", "wt") as f:
                json.dump(out, f)

        manifest = {f"Polymarket_Window/{d}.hlxz" for d in DAYS} | {"Kalshi_Yes_Trade/20261001.hlxz"}
        failures = {"Kalshi_Yes_Trade/20261001.hlxz"}
        inv = inventory.build(TAPE, helix_dir, res_path, failures, manifest)
        (HERE / "inventory.json").write_text(json.dumps(clean(inv), indent=0))

        for day in DAYS:
            v = {"identity": validate.check_identity(day, TAPE)}
            v |= validate.check_day(day, TAPE, 0, tmp)
            v.pop("seconds")
            (HERE / f"validate_{day}.json").write_text(json.dumps(clean(v), indent=0))

        # The studies on hand-made day roles (two days are all holdout under the
        # real rule, which keeps the most recent 14).
        inv_path = data / "helix_inventory.json"
        roles = {d: {"role": "discovery", "usable": True, "chainlink": True} for d in DAYS}
        inv_path.write_text(json.dumps({"days": roles}))
        sys.argv = ["x", "--split", "discovery", "--data", str(helix_dir), "--inventory", str(inv_path),
                    "--resolutions", str(res_path)]  # fmt: skip
        helix_sweeps._main()
        sweeps = json.loads((data / "helix_sweeps_discovery.json").read_text())
        (HERE / "sweeps.json").write_text(json.dumps(sweeps, indent=0))
        helix_fairvalue._main()
        fair = json.loads((data / "helix_fairvalue_discovery.json").read_text())
        (HERE / "fairvalue.json").write_text(json.dumps(fair, indent=0))

        sys.argv = ["x", "--days", ",".join(DAYS), "--root", str(TAPE), "--bin-ms", "50",
                    "--out", str(data / "leadlag.json")]  # fmt: skip
        helix_leadlag._main()
        (HERE / "leadlag.json").write_text((data / "leadlag.json").read_text())


if __name__ == "__main__":
    main()
