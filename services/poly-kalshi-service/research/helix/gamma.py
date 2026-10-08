"""Polymarket's own resolution per window, from Gamma, cached on disk.

One request per window (``/events?slug=<asset>-updown-15m-<open>``), sent with
our own user agent (Gamma answers Python's default with 403). Answers are kept
in ``<dir>/outcomes.json`` so a window is fetched once, ever.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from poly_kalshi_service.windows import USER_AGENT, poly_slug

GAMMA = "https://gamma-api.polymarket.com"


class GammaCache:
    def __init__(self, directory: Path, min_interval_s: float = 0.15) -> None:
        self.path = directory / "outcomes.json"
        self.data: dict[str, str | None] = (
            json.loads(self.path.read_text()) if self.path.is_file() else {}
        )
        self._http = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0)
        self._min_interval = min_interval_s
        self._last = 0.0

    def outcome(self, asset: str, open_unix: int) -> str | None:
        """``"yes"`` (Up) / ``"no"`` (Down) once resolved; None if not resolved or unknown."""
        key = f"{asset}:{open_unix}"
        if self.data.get(key) is not None:
            return self.data[key]
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            response = self._http.get(
                f"{GAMMA}/events", params={"slug": poly_slug(asset, open_unix)}
            )
            response.raise_for_status()
            events = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        result = None
        if events and events[0].get("markets"):
            market = events[0]["markets"][0]
            prices = [float(p) for p in json.loads(market.get("outcomePrices") or "[]")]
            if market.get("closed") and prices == [1.0, 0.0]:
                result = "yes"
            elif market.get("closed") and prices == [0.0, 1.0]:
                result = "no"
        self.data[key] = result
        return result

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=0, sort_keys=True))
