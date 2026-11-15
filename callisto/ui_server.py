"""Read-only status page for the Callisto monitor. Stdlib only.

Binds to 127.0.0.1 by default, to be published through cloudflared with access control
(or reached with `ssh -L 8079:127.0.0.1:8079 ...`).

Routes
  GET /            ui/index.html
  GET /api/state   heartbeat, settings, signals, open ideas, recent events, backtest summary

Env
  UI_HOST=127.0.0.1  UI_PORT=8079
  UI_USER / UI_PASSWORD   optional HTTP basic auth
  DATA_DIR                same data dir the monitor writes (default callisto/data)
"""
from __future__ import annotations

import base64
import hmac
import json
import os
from collections import Counter
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import CFG

HERE = Path(__file__).resolve().parent
INDEX = HERE / "ui" / "index.html"
DATA = CFG.data_dir
USER, PASSWORD = os.environ.get("UI_USER", ""), os.environ.get("UI_PASSWORD", "")


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001 - missing or half-written file
        return default


def read_events(limit: int = 5000) -> list[dict]:
    p = DATA / "events.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_jsonl(p: Path, limit: int = 2000) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def state() -> dict:
    events = read_events()
    signals = [e for e in events if e.get("event") in ("signal", "too_old")]
    activity = [e for e in events if e.get("event") != "ignored"][-150:]
    kinds = Counter(e.get("kind") or e.get("event") for e in events)
    mon = read_json(DATA / "monitor_state.json", {})
    return {
        "now": datetime.now(UTC).isoformat(timespec="seconds"),
        "heartbeat": read_json(DATA / "heartbeat.json", {}),
        "settings": {
            "channel": CFG.channel, "model": CFG.openai_model,
            "sl_pips": CFG.sl_pips, "tp_pips": CFG.tp_pips,
            "trail_start_pips": CFG.trail_start_pips,
            "trail_distance_pips": CFG.trail_distance_pips,
            "trail_step_pips": CFG.trail_step_pips,
            "max_age_pips": CFG.max_age_pips, "group_hours": CFG.group_hours,
            "poll_seconds": CFG.poll_seconds,
            "webhook": bool(CFG.webhook_url),
            "telegram_bot": bool(CFG.telegram_bot_token and CFG.telegram_chat_id),
        },
        "counts": dict(kinds),
        "signals": signals[::-1][:100],
        "open_ideas": mon.get("ideas", [])[::-1],
        "activity": activity[::-1],
        "backtest": read_json(DATA / "results" / "summary.json", None),
        "executor": read_json(DATA / "executor_heartbeat.json", None),
        "executions": read_jsonl(DATA / "executions.jsonl")[-60:][::-1],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _auth_ok(self) -> bool:
        if not (USER and PASSWORD):
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            u, _, p = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:  # noqa: BLE001
            return False
        return hmac.compare_digest(u, USER) and hmac.compare_digest(p, PASSWORD)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if not self._auth_ok():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="callisto"')
            self.end_headers()
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(state(), default=str).encode(), "application/json")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")


def main():
    host = os.environ.get("UI_HOST", "127.0.0.1")
    port = int(os.environ.get("UI_PORT", "8079"))
    print(f"callisto ui on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
