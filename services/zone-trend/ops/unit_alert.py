"""Alert when a zone-trend systemd unit fails: ``OnFailure=zone-trend-unit-alert@%n.service``.

A process that cannot start cannot alert on itself, so systemd runs this
instead. Standard library only, run by the system ``/usr/bin/python3``, so it
works when the venv or the service configuration is what broke. Reads the
``.env`` file leniently (``KEY=VALUE`` lines), sends through notification-service
(``NOTIFICATION_*``) to ``ZT_CRITICAL_CHANNELS``, at most once per unit per
``--cooldown-minutes``, and always exits 0.

    python3 unit_alert.py zone-trend-live.service --env-file /home/zt/.../.env.zt
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return env


def _headers(secret: str) -> dict[str, str]:
    # Both forms, as ta-notify sends: the service has accepted either.
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
        headers["X-API-Key"] = secret
    return headers


def last_lines(unit: str, n: int = 15) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return out.stdout[-1500:]
    except (OSError, subprocess.SubprocessError):
        return "(journal unavailable)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("unit")
    ap.add_argument("--env-file", type=Path, required=True)
    ap.add_argument("--cooldown-minutes", type=float, default=30)
    ap.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("STATE_DIRECTORY", "/tmp/zt-unit-alert")),
    )
    args = ap.parse_args()
    try:
        args.state_dir.mkdir(parents=True, exist_ok=True)
        stamp = args.state_dir / f"{args.unit}.json"
        state = json.loads(stamp.read_text()) if stamp.exists() else {"last": 0, "skipped": 0}
        now = time.time()
        if now - state["last"] < args.cooldown_minutes * 60:
            state["skipped"] += 1
            stamp.write_text(json.dumps(state))
            return 0
        env = read_env(args.env_file)
        url = env.get("NOTIFICATION_SERVICE_URL", "http://127.0.0.1:3010").rstrip("/")
        channels = [
            c.strip().upper()
            for c in env.get("ZT_CRITICAL_CHANNELS", "TELEGRAM").split(",")
            if c.strip()
        ]
        lines = [f"host {socket.gethostname()}", f"unit {args.unit} failed"]
        if state["skipped"]:
            lines.append(f"({state['skipped']} more failures since the last alert)")
        lines.append(last_lines(args.unit))
        body = json.dumps(
            {
                "subject": f"🚨 zone-trend: {args.unit} failed",
                "message": "\n".join(lines),
                "contentType": "text",
                "channels": channels,
                "source": "zone-trend-unit-alert",
            }
        ).encode()
        req = urllib.request.Request(
            f"{url}/notifications",
            data=body,
            method="POST",
            headers=_headers(env.get("NOTIFICATION_API_KEY", "")),
        )
        urllib.request.urlopen(req, timeout=20).read()  # noqa: S310 - our own service URL
        stamp.write_text(json.dumps({"last": now, "skipped": 0}))
    except Exception as exc:  # noqa: BLE001 - never fail: a broken alert path adds nothing
        print(f"unit alert failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
