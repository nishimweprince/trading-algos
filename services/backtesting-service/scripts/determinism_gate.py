"""Determinism gate for the backtesting-service migration.

Runs a fixed set of backtests against the committed XAUUSD candles through the
HTTP API, hashes each report, and compares against
``scripts/determinism_baseline.json``. The pre-move and post-move runs must
agree byte for byte; anything else means the migration changed behaviour.

**Exits non-zero on any mismatch, any missing case, or any non-200 response.**
It used to print and continue, which made it impossible to wire into CI: a run
where every case 404'd still exited 0.

Takes the import root as argv[1] so the same script drives session-hedging
(flat modules) and backtesting-service (a package).

Deliberate behaviour change is fine, but it is a decision, not an accident:
rerun with ``--update-baseline``, commit the regenerated JSON in the *same*
commit as the change, and record the cause in migration-spec.md.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

# Resolved before the chdir below, because argv[1] moves the working directory
# out from under any relative path.
_HERE = Path(__file__).resolve().parent
BASELINE_PATH = _HERE / "determinism_baseline.json"
ENV_PATH = _HERE / "determinism.env"

argv = [a for a in sys.argv[1:] if not a.startswith("--")]
update_baseline = "--update-baseline" in sys.argv

root = argv[0]
os.chdir(root)
sys.path.insert(0, "src")
sys.path.insert(0, ".")

if len(argv) > 1 and argv[1] == "package":
    from backtesting_service.api import create_app
    from backtesting_service.config import Settings
else:
    from api import create_app
    from config import Settings

# noqa: E402 is deliberate for the whole block above and below — the import root
# has to be chosen and sys.path set before any service module is importable.
from fastapi.testclient import TestClient  # noqa: E402

# Deliberately NOT load_settings(): that reads the developer's own gitignored
# .env, which is how four hashes ended up published that only one machine could
# reproduce. It also chdirs to the env file's parent, which would move the
# working directory off the import root set above.
settings = Settings(_env_file=ENV_PATH, _env_file_encoding="utf-8")
app = create_app(settings=settings)

LOCAL = {"symbol": "XAUUSD", "source": "local"}

CASES = [
    {"name": "hedge_pair_M15", "body": {**LOCAL, "timeframe": "M15"}},
    {
        "name": "synthetic_breakout",
        "body": {**LOCAL, "timeframe": "M15", "entry_mode": "synthetic_breakout"},
    },
    {"name": "rr2_M15", "body": {**LOCAL, "timeframe": "M15", "rr": 2.0}},
    {"name": "H1", "body": {**LOCAL, "timeframe": "H1"}},
]

baseline = json.loads(BASELINE_PATH.read_text())["cases"]

key = settings.api_key.get_secret_value() if settings.api_key else None
headers = {"X-API-Key": key} if key else {}

observed: dict[str, dict] = {}
failures: list[str] = []

with TestClient(app) as client:
    for case in CASES:
        name = case["name"]
        response = client.post("/v1/backtests", json=case["body"], headers=headers)
        if response.status_code != 200:
            print(f"{name:20} FAIL  HTTP {response.status_code} {response.text[:200]}")
            failures.append(f"{name}: HTTP {response.status_code}")
            continue

        body = response.json()
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        trades = body.get("trades")
        n = len(trades) if isinstance(trades, list) else body.get("metrics", {}).get("trades")
        observed[name] = {"sha256": digest, "bytes": len(canonical), "trades": n}

        expected = baseline.get(name)
        if expected is None:
            status = "NEW "
            failures.append(f"{name}: absent from the baseline")
        elif expected["sha256"] == digest:
            status = "ok  "
        else:
            status = "FAIL"
            failures.append(f"{name}: expected {expected['sha256'][:16]}..., got {digest[:16]}...")
        print(f"{name:20} {status}  sha256={digest}  bytes={len(canonical)}  trades={n}")

missing = [c["name"] for c in CASES if c["name"] not in observed]

if update_baseline:
    payload = json.loads(BASELINE_PATH.read_text())
    payload["cases"] = observed
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nBaseline rewritten: {BASELINE_PATH}")
    print("Commit it WITH the change that moved the hashes, and record the cause")
    print("in migration-spec.md. An unexplained hash change is a regression.")
    sys.exit(0)

total = sum(o["trades"] for o in observed.values() if isinstance(o["trades"], int))
print(f"\n{len(observed)}/{len(CASES)} cases hashed, {total} trades total")

if failures or missing:
    for line in failures:
        print(f"  - {line}")
    for name in missing:
        print(f"  - {name}: no report produced")
    sys.exit(1)

print("Determinism gate passed.")
