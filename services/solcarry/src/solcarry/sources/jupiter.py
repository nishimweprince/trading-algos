"""Jupiter quote API: point-in-time LST -> SOL exit depth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..http import Http

WSOL = "So11111111111111111111111111111111111111112"
DECIMALS = 9


@dataclass(frozen=True)
class Quote:
    in_amount: int
    out_amount: int
    price_impact_pct: float
    routes: str


def parse_quote(js: dict[str, Any]) -> Quote:
    labels = []
    for leg in js.get("routePlan", []) or []:
        info = leg.get("swapInfo", {})
        labels.append(f"{info.get('label', '?')}:{leg.get('percent', '?')}%")
    return Quote(
        in_amount=int(js["inAmount"]),
        out_amount=int(js["outAmount"]),
        price_impact_pct=float(js.get("priceImpactPct") or 0.0),
        routes=" + ".join(labels),
    )


class Jupiter:
    def __init__(self, cfg: Config, http: Http) -> None:
        self.cfg = cfg
        self.http = http

    def quote(self, input_mint: str, amount_tokens: float) -> Quote | None:
        raw = int(round(amount_tokens * 10**DECIMALS))
        r = self.http.get(
            self.cfg.sources.jupiter_quote,
            params={
                "inputMint": input_mint,
                "outputMint": WSOL,
                "amount": raw,
                "slippageBps": 100,
            },
        )
        if r.status_code != 200:
            return None
        return parse_quote(r.json())
