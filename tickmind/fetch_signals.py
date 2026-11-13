"""Snapshot the public /api/signals list to CSV (no login needed).

Known typos in the site's data are kept raw here; backtest_signals.clean() fixes them.
Usage: python fetch_signals.py [out.csv]
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.request

URL = "https://tickmindai.com/api/signals"
COLS = ["id", "posted_unix", "pair", "side", "entry", "stop", "target", "status", "result_pips"]


def fetch() -> list[dict]:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (signal-research)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["signals"]


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "signals_raw.csv"
    sigs = fetch()
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for s in sigs:
            w.writerow([s["id"], s["postedAtUnix"], s.get("pair", "XAUUSD"), s["side"], s["entry"],
                        s["stop"], s["target"], s["status"], s.get("result", "")])
    print(f"{len(sigs)} signals -> {out}")


if __name__ == "__main__":
    main()
