"""PLAN 0.1b: every window's settlement, from the venues, joined to the tape.

    python -m research.helix.resolutions [--out research/data]

Writes ``resolutions.parquet``, one row per (asset, open_unix) over the Helix
days, with:

- **Polymarket** (Gamma, paged by series and day): ``poly_outcome`` (Up =
  ``yes``), ``poly_source`` (the market's ``resolutionSource``: the Chainlink
  spot stream ``…/<asset>-usd`` before the switch, the 60 s TWAP stream
  ``…/<asset>-usd-twap-60s-streams`` after it), ``regime`` (``spot`` or
  ``twap``), and the window's ``fee_rate``/``fee_exponent``/``fees_enabled``.
- **Kalshi** (``/markets`` settled, paged by series and day): ``kalshi_outcome``,
  ``kalshi_strike`` (``floor_strike``) and ``kalshi_close_value``.
- **Tape** (Helix Window stream): ``tape_strike``, ``tape_close`` (the
  resolution record's price, i.e. the next window's strike) and
  ``tape_outcome`` = ``tape_close >= tape_strike``. This is Helix's own
  derivation, not a settlement: it disagrees with Polymarket on some windows,
  and ``poly_outcome`` is what every replay settles on.

Responses are cached per (venue, asset, day) under ``<out>/venue_cache/``, so a
rerun fetches nothing it already has.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from poly_kalshi_service.windows import USER_AGENT

from . import reader

GAMMA = "https://gamma-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY_SERIES = {a: f"{a.lower()}-up-or-down-15m" for a in reader.ASSETS}


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_bounds(day: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(day, "%Y%m%d").replace(tzinfo=UTC)
    return start, start + timedelta(days=1)


class Fetcher:
    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.http = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0)
        self.series_ids: dict[str, str] = {}

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        for attempt in range(6):
            try:
                response = self.http.get(url, params=params)
                if response.status_code == 429:
                    raise httpx.HTTPStatusError("429", request=response.request, response=response)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError):
                if attempt == 5:
                    raise
                time.sleep(0.5 * 2**attempt)
        return None

    def _cached(self, name: str, fetch) -> Any:
        p = self.cache / f"{name}.json"
        if p.is_file():
            return json.loads(p.read_text())
        body = fetch()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body))
        return body

    def series_id(self, asset: str) -> str:
        if asset not in self.series_ids:
            body = self._cached(
                f"series_{asset}",
                lambda: self._get(f"{GAMMA}/series", {"slug": POLY_SERIES[asset]}),
            )
            self.series_ids[asset] = str(body[0]["id"])
        return self.series_ids[asset]

    def poly_day(self, asset: str, day: str) -> list[dict[str, Any]]:
        """Windows closing on ``day`` (by end date), one compact row each."""
        start, end = _day_bounds(day)

        def fetch() -> list[dict[str, Any]]:
            rows, offset = [], 0
            while True:
                page = self._get(
                    f"{GAMMA}/events",
                    {
                        "series_id": self.series_id(asset),
                        "end_date_min": _iso(start + timedelta(seconds=1)),
                        "end_date_max": _iso(end),
                        "limit": 200,
                        "offset": offset,
                    },
                )
                for e in page:
                    for m in e.get("markets") or []:
                        rows.append(
                            {
                                "slug": e.get("slug"),
                                "outcomePrices": m.get("outcomePrices"),
                                "closed": m.get("closed"),
                                "resolutionSource": m.get("resolutionSource"),
                                "feeSchedule": m.get("feeSchedule"),
                                "feesEnabled": m.get("feesEnabled"),
                                "outcomes": m.get("outcomes"),
                            }
                        )
                if len(page) < 200:
                    return rows
                offset += 200

        return self._cached(f"poly_{asset}_{day}", fetch)

    def kalshi_day(self, asset: str, day: str) -> list[dict[str, Any]]:
        start, end = _day_bounds(day)

        def fetch() -> list[dict[str, Any]]:
            rows, cursor = [], None
            while True:
                params: dict[str, Any] = {
                    "series_ticker": f"KX{asset}15M",
                    "min_close_ts": int(start.timestamp()) + 1,
                    "max_close_ts": int(end.timestamp()),
                    "limit": 1000,
                }
                if cursor:
                    params["cursor"] = cursor
                body = self._get(f"{KALSHI}/markets", params)
                for m in body.get("markets") or []:
                    rows.append(
                        {
                            k: m.get(k)
                            for k in (
                                "ticker",
                                "open_time",
                                "close_time",
                                "result",
                                "status",
                                "floor_strike",
                                "expiration_value",
                            )
                        }  # fmt: skip
                    )
                cursor = body.get("cursor")
                if not cursor:
                    return rows

        return self._cached(f"kalshi_{asset}_{day}", fetch)


def _poly_outcome(row: dict[str, Any]) -> str | None:
    try:
        prices = [float(p) for p in json.loads(row.get("outcomePrices") or "[]")]
        outcomes = [o.lower() for o in json.loads(row.get("outcomes") or "[]")]
    except (TypeError, ValueError):
        return None
    if not row.get("closed") or outcomes != ["up", "down"]:
        return None
    return {(1.0, 0.0): "yes", (0.0, 1.0): "no"}.get(tuple(prices))


def _number(value: Any) -> float | None:
    """Kalshi sends ``expiration_value`` as text, sometimes with a thousands separator."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _regime(source: str | None) -> str | None:
    if not source:
        return None
    return "twap" if "twap" in source.lower() else "spot"


def build(days: list[str], cache: Path, root: Path | None) -> list[dict[str, Any]]:
    fetcher = Fetcher(cache)
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    for day in days:
        windows = {
            (reader.HELIX_15M[w.asset], w.open_unix): w for w in reader.iter_windows(day, root)
        }
        tape_res = {
            (reader.HELIX_15M[r.asset], r.open_unix): r
            for r in reader.iter_resolutions(day, root)
            if r.asset in reader.HELIX_15M
        }
        for (asset, open_unix), w in windows.items():
            r = tape_res.get((asset, open_unix))
            strike = w.strike if w.strike == w.strike and w.strike > 0 else None
            rows[(asset, open_unix)] = {
                "asset": asset,
                "open_unix": open_unix,
                "day": day,
                "tape_strike": strike,
                "tape_close": None if r is None else r.close_px,
                "tape_outcome": None
                if r is None or strike is None
                else ("yes" if r.close_px >= strike else "no"),
            }
        for asset in reader.ASSETS:
            for m in fetcher.poly_day(asset, day):
                slug = m.get("slug") or ""
                try:
                    open_unix = int(slug.rsplit("-", 1)[1])
                except (IndexError, ValueError):
                    continue
                row = rows.setdefault(
                    (asset, open_unix), {"asset": asset, "open_unix": open_unix, "day": day}
                )
                fees = m.get("feeSchedule") or {}
                row |= {
                    "poly_outcome": _poly_outcome(m),
                    "poly_source": m.get("resolutionSource"),
                    "regime": _regime(m.get("resolutionSource")),
                    "fees_enabled": m.get("feesEnabled"),
                    "fee_rate": fees.get("rate") if m.get("feesEnabled", True) else 0.0,
                    "fee_exponent": fees.get("exponent"),
                }
            for m in fetcher.kalshi_day(asset, day):
                close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
                open_unix = int(close.timestamp()) - 900
                row = rows.setdefault(
                    (asset, open_unix), {"asset": asset, "open_unix": open_unix, "day": day}
                )
                row |= {
                    "kalshi_ticker": m.get("ticker"),
                    "kalshi_outcome": m.get("result") if m.get("result") in ("yes", "no") else None,
                    "kalshi_strike": m.get("floor_strike"),
                    "kalshi_close_value": _number(m.get("expiration_value")),
                }
        print(day, len(rows), flush=True)
    return sorted(rows.values(), key=lambda r: (r["open_unix"], r["asset"]))


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = by_day.setdefault(
            r["day"], {"windows": 0, "regimes": {}, "fee_rates": {}, "poly_known": 0,
                       "tape_vs_poly_disagree": 0, "kalshi_vs_poly_disagree": 0, "both": 0}
        )  # fmt: skip
        d["windows"] += 1
        reg = r.get("regime")
        d["regimes"][str(reg)] = d["regimes"].get(str(reg), 0) + 1
        fr = r.get("fee_rate")
        d["fee_rates"][str(fr)] = d["fee_rates"].get(str(fr), 0) + 1
        p = r.get("poly_outcome")
        if p:
            d["poly_known"] += 1
            if r.get("tape_outcome") and r["tape_outcome"] != p:
                d["tape_vs_poly_disagree"] += 1
            if r.get("kalshi_outcome"):
                d["both"] += 1
                d["kalshi_vs_poly_disagree"] += r["kalshi_outcome"] != p
    switch = [
        day for day, d in sorted(by_day.items())
        if d["regimes"].get("twap", 0) > d["regimes"].get("spot", 0)
    ]  # fmt: skip
    return {"first_twap_day": switch[0] if switch else None, "days": by_day}


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.helix.resolutions")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("research/data"))
    args = parser.parse_args()
    import pandas as pd

    days = reader.days("Polymarket_Window", args.root)
    rows = build(days, args.out / "venue_cache", args.root)
    args.out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(args.out / "resolutions.parquet", index=False)
    summary = summarise(rows)
    (args.out / "resolutions_summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "days"}, indent=1))


if __name__ == "__main__":
    _main()
