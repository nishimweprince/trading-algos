"""Alert when a systemd unit fails: ``OnFailure=ofi-unit-alert@%n.service``.

A service that cannot start never sends an alert of its own (2026-10-02: the
scalper crash-looped for an hour on a config error, silently). systemd runs
this instead, each time the unit fails.

It deliberately depends on nothing but the standard library and is run by the
system ``/usr/bin/python3``, so it still works when what broke is the venv, a
dependency, or the service's own configuration. For the same reason it reads
the ``.env`` file leniently (plain ``KEY=VALUE`` lines) instead of validating it.

- **Sends** through notification-service (``NOTIFICATION_*``); if that fails or
  is not configured, straight to Telegram with the scalper's own bot
  (``OFI_TELEGRAM_BOT_TOKEN`` to ``OFI_TELEGRAM_ADMIN_USER_IDS``). So a failure
  of notification-service itself is reported too.
- **Rate-limited** per unit (``--cooldown-minutes``, default 30): a crash loop
  restarting every few seconds gives one alert, and the next one says how many
  failures were not reported in between.
- **Never fails**: it always exits 0, so a broken alert path cannot add a
  second failed unit to the one being reported.

    ofi-unit-alert@ofi-scalper-service.service  ->
    python3 unit_alert.py ofi-scalper-service.service --env-file .../.env.dev
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["build_message", "main", "read_env", "should_send"]

SOURCE = "ofi-scalper.unit-alert"
JOURNAL_LINES = 12
TIMEOUT_SECONDS = 10
TELEGRAM_LIMIT = 3900  # under Telegram's 4096-character message limit

Post = Callable[[str, bytes, dict[str, str]], int]


def read_env(path: Path | None) -> dict[str, str]:
    """``KEY=VALUE`` lines; comments, blanks and malformed lines are skipped."""
    values: dict[str, str] = {}
    if path is None:
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"({type(exc).__name__})"
    return done.stdout.strip()


def unit_facts(unit: str) -> dict[str, str]:
    raw = _run(
        [
            "systemctl",
            "show",
            unit,
            "--property=ActiveState,SubState,Result,NRestarts,ExecMainStatus",
        ]
    )
    facts = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    facts["journal"] = _run(
        ["journalctl", "-u", unit, "-n", str(JOURNAL_LINES), "--no-pager", "-o", "cat"]
    )
    return facts


def should_send(state_path: Path, now: float, cooldown_seconds: float) -> tuple[bool, int]:
    """(send?, failures suppressed since the last alert). Updates the state file."""
    state: dict[str, Any] = {}
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    last = state.get("last_sent")  # None: never alerted for this unit
    suppressed = int(state.get("suppressed", 0))
    if last is not None and now - float(last) < cooldown_seconds:
        state["suppressed"] = suppressed + 1
        send = False
    else:
        state = {"last_sent": now, "suppressed": 0}
        send = True
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))
    except OSError:
        pass
    return send, suppressed


def build_message(
    unit: str, facts: dict[str, str], *, host: str, suppressed: int
) -> tuple[str, list[str]]:
    subject = f"{unit} FAILED on {host}"
    lines = [
        f"state: {facts.get('ActiveState', '?')}/{facts.get('SubState', '?')}, "
        f"result {facts.get('Result', '?')}, exit status {facts.get('ExecMainStatus', '?')}, "
        f"restarts {facts.get('NRestarts', '?')}",
    ]
    if suppressed:
        lines.append(f"{suppressed} more failure(s) since the last alert were not reported")
    journal = facts.get("journal", "").strip()
    if journal:
        lines += ["", "Last log lines:", journal]
    lines += [
        "",
        f"Check: journalctl -u {unit} -n 50 --no-pager",
        "A start alert follows once it runs again.",
    ]
    return subject, lines


def _post(url: str, body: bytes, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError):
        return 0


def send_notification_service(
    env: dict[str, str], subject: str, lines: list[str], key: str, post: Post
) -> bool:
    url = env.get("NOTIFICATION_SERVICE_URL", "").strip()
    enabled = env.get("NOTIFICATIONS_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
    channels = [c.strip() for c in env.get("NOTIFICATION_CHANNELS", "").split(",") if c.strip()]
    if not url or not enabled or not channels:
        return False
    headers = {"Content-Type": "application/json"}
    api_key = env.get("NOTIFICATION_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-Key"] = api_key
    payload = {
        "subject": subject,
        "message": "\n".join(lines),
        "contentType": "text",
        "channels": sorted(channels),
        "source": SOURCE,
        "idempotencyKey": key,
    }
    status = post(f"{url.rstrip('/')}/notifications", json.dumps(payload).encode(), headers)
    return 200 <= status < 300


def send_telegram(
    env: dict[str, str],
    subject: str,
    lines: list[str],
    post: Post,
    base_url: str = "https://api.telegram.org",
) -> bool:
    token = env.get("OFI_TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in env.get("OFI_TELEGRAM_ADMIN_USER_IDS", "").split(",") if c.strip()]
    if not token or not chats:
        return False
    text = (f"{subject}\n\n" + "\n".join(lines))[:TELEGRAM_LIMIT]
    sent = False
    for chat in chats:
        body = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True})
        status = post(
            f"{base_url}/bot{token}/sendMessage",
            body.encode(),
            {"Content-Type": "application/json"},
        )
        sent = sent or 200 <= status < 300
    return sent


def main(
    argv: list[str] | None = None,
    *,
    post: Post = _post,
    facts: Callable[[str], dict[str, str]] = unit_facts,
    now: Callable[[], float] = time.time,
) -> int:
    parser = argparse.ArgumentParser(description="Alert that a systemd unit failed")
    parser.add_argument("unit")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--cooldown-minutes", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        state_dir = args.state_dir or Path(
            os.environ.get("STATE_DIRECTORY", "/tmp/ofi-unit-alert")  # noqa: S108
        )
        moment = now()
        send, suppressed = should_send(
            state_dir / f"{args.unit}.json", moment, args.cooldown_minutes * 60
        )
        if not send:
            print(f"{args.unit}: failure noted, alert suppressed (cooldown)")
            return 0
        env = read_env(args.env_file)
        subject, lines = build_message(
            args.unit, facts(args.unit), host=socket.gethostname(), suppressed=suppressed
        )
        key = f"unit-alert:{args.unit}:{int(moment)}"
        if send_notification_service(env, subject, lines, key, post):
            print(f"{args.unit}: alert sent via notification-service")
        elif send_telegram(env, subject, lines, post):
            print(f"{args.unit}: alert sent via Telegram (notification-service unavailable)")
        else:
            print(f"{args.unit}: alert NOT sent: no working channel", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never fail on top of the failure
        print(f"{args.unit}: alert error {type(exc).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
