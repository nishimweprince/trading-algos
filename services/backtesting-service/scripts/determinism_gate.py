"""Determinism gate for the backtesting-service migration.

Runs a fixed set of backtests against local candle data through the HTTP API and
prints a stable hash of each report. The pre-move and post-move runs must agree
byte for byte; anything else means the migration changed behaviour.

Takes the import root as argv[1] so the same script drives session-hedging
(flat modules) and backtesting-service (a package).
"""

import hashlib
import json
import os
import sys

root = sys.argv[1]
os.chdir(root)
sys.path.insert(0, "src")
sys.path.insert(0, ".")

if len(sys.argv) > 2 and sys.argv[2] == "package":
    from backtesting_service.api import create_app
    from backtesting_service.config import load_settings
else:
    from api import create_app
    from config import load_settings

from fastapi.testclient import TestClient

settings = load_settings()
app = create_app(settings=settings)

CASES = [
    {"name": "hedge_pair_M15", "body": {"symbol": "XAUUSD", "timeframe": "M15", "source": "local"}},
    {"name": "synthetic_breakout", "body": {"symbol": "XAUUSD", "timeframe": "M15", "source": "local",
                                        "entry_mode": "synthetic_breakout"}},
    {"name": "rr2_M15", "body": {"symbol": "XAUUSD", "timeframe": "M15", "source": "local",
                                  "rr": 2.0}},
    {"name": "H1", "body": {"symbol": "XAUUSD", "timeframe": "H1", "source": "local"}},
]

key = settings.api_key.get_secret_value() if settings.api_key else None
headers = {"X-API-Key": key} if key else {}

with TestClient(app) as client:
    for case in CASES:
        response = client.post("/v1/backtests", json=case["body"], headers=headers)
        if response.status_code != 200:
            print(f"{case['name']:20} HTTP {response.status_code} {response.text[:200]}")
            continue
        body = response.json()
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        trades = body.get("trades")
        n = len(trades) if isinstance(trades, list) else body.get("metrics", {}).get("trades")
        print(f"{case['name']:20} sha256={digest}  bytes={len(canonical)}  trades={n}")
