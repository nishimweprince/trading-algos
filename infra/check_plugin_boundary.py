"""Fail when application code builds a broker connection itself.

Services choose a broker or exchange by name through
``ta_plugin_api.load_providers`` and get every provider, gateway and terminal
from the discovered factory. They may import a plugin's settings mixin, error
types, testing fakes or type names; what they may not do is construct one of
the objects below directly, or import a broker SDK. Either would bypass
discovery, and with it the fail-closed checks and the per-host choice of
plugins (a macOS host never installs MetaTrader5).

Run from the repository root: ``python3 infra/check_plugin_boundary.py``.
Exits 1 and lists every violation.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Application code: services and the adopters that consume the platform.
SCANNED = ["services/*/src", "ipda/src", "lookup-trader/server"]

FORBIDDEN_CONSTRUCTORS = frozenset(
    {
        "AccountReader",
        "BinanceFuturesMarketData",
        "BinanceMarketData",
        "CTraderExecution",
        "CTraderGateway",
        "CTraderMarketData",
        "FapiRest",
        "FuturesStreams",
        "MT5Execution",
        "MT5MarketData",
        "MT5Oco",
        "RealMT5Adapter",
    }
)
FORBIDDEN_IMPORTS = frozenset({"MetaTrader5", "binance", "ctrader_open_api"})


def _files() -> Iterator[Path]:
    for pattern in SCANNED:
        for base in ROOT.glob(pattern):
            yield from (p for p in base.rglob("*.py") if "tests" not in p.parts)


def _violations(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(ROOT)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in FORBIDDEN_CONSTRUCTORS:
                yield f"{where}:{node.lineno}: constructs {name}; use the load_providers factory"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    yield f"{where}:{node.lineno}: imports {alias.name}; only plugins may"
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in FORBIDDEN_IMPORTS:
                yield f"{where}:{node.lineno}: imports {node.module}; only plugins may"


def main() -> int:
    problems = [problem for path in _files() for problem in _violations(path)]
    for problem in problems:
        print(problem)
    scanned = sum(1 for _ in _files())
    print(f"{len(problems)} plugin-boundary violation(s) in {scanned} files")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
