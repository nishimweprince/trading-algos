"""strategies.yaml loader."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

TF_MIN = {"5min": 5, "10min": 10, "15min": 15}
HTF_MIN = {"1H": 60, "4H": 240, "none": None}


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    symbol: str
    enabled: bool
    engine: str  # patterns | hs | hs_confluence | patterns_confluence | rule_C1 | rule_C2 | rule_C3
    timeframes: tuple[int, ...]
    patterns: tuple[str, ...]
    pivot_k: int | None
    trend_filter: int | None  # HTF minutes or None
    qualification: str  # risk_atr_and_3x_cost | risk_atr_only | none
    confluence_window: int | None
    stop_pips: float
    take_profit_pips: float
    lots: float | None
    source: str  # execution-service source slug == cTrader label
    backtest: dict = field(default_factory=dict)

    @property
    def side_filter(self) -> int | None:
        """C4/C5 trade only inverse H&S longs; everything else lets the detector decide."""
        if self.engine == "hs" and self.patterns == ("inverse_hs",):
            return 1
        return None


@dataclass(frozen=True)
class Config:
    defaults: dict
    pip: dict[str, float]
    cost: dict[str, float]
    strategies: tuple[Strategy, ...]

    def by_symbol(self, symbol: str, enabled_only: bool = True) -> list[Strategy]:
        return [s for s in self.strategies if s.symbol == symbol and (s.enabled or not enabled_only)]

    @property
    def symbols(self) -> list[str]:
        return sorted({s.symbol for s in self.strategies})


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    out = []
    for s in raw["strategies"]:
        out.append(Strategy(
            id=s["id"], name=s["name"], symbol=s["symbol"], enabled=bool(s["enabled"]), engine=s["engine"],
            timeframes=tuple(TF_MIN[x] for x in s["timeframes"]), patterns=tuple(s.get("patterns") or ()),
            pivot_k=s.get("pivot_k"), trend_filter=HTF_MIN[s.get("trend_filter") or "none"],
            qualification=s["qualification"], confluence_window=s.get("confluence_window_minutes"),
            stop_pips=float(s["stop_pips"]), take_profit_pips=float(s["take_profit_pips"]),
            lots=None if s.get("lots") is None else float(s["lots"]),
            source=s.get("source") or "wf_" + s["id"].lower(), backtest=s.get("backtest") or {},
        ))
    return Config(defaults=raw["defaults"], pip=raw["pip_size"], cost=raw["research_cost"], strategies=tuple(out))
