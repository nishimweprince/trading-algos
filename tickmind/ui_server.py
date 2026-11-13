"""Tiny monitoring UI for the Tickmind watcher + claims-vs-replay backtest.

Stdlib only. Binds to 127.0.0.1 by default so it is reached through cloudflared
(`cloudflared tunnel --url http://127.0.0.1:8077`, or a named tunnel).

Routes
  GET  /             ui/index.html
  GET  /api/state    summary.json + watcher heartbeat + active signals + recent events
  POST /api/refresh  re-fetch /api/signals and re-run the backtest now

Env
  UI_HOST=127.0.0.1  UI_PORT=8077
  UI_USER / UI_PASSWORD   optional HTTP basic auth (set it if the tunnel is public)
  DATA_DIR                HistData XAUUSD M1 folder; refresh is skipped without it
  REFRESH_HOURS=6         periodic refresh of signals + backtest
  PYTHON                  interpreter for the refresh subprocesses (default: this one)
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("TICKMIND_STATE_DIR", HERE / "state"))
RESULTS = HERE / "results"
INDEX = HERE / "ui" / "index.html"
PY = os.environ.get("PYTHON", sys.executable)
DATA_DIR = os.environ.get("DATA_DIR", "")
REFRESH_S = float(os.environ.get("REFRESH_HOURS", "6")) * 3600
USER, PASSWORD = os.environ.get("UI_USER", ""), os.environ.get("UI_PASSWORD", "")

_refresh_lock = threading.Lock()
_refresh = {"running": False, "last_utc": None, "ok": None, "log": ""}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def tail_events(n: int = 150) -> list[dict]:
    p = STATE / "events.jsonl"
    if not p.exists():
        return []
    lines = p.read_text().splitlines()[-n:]
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def run_refresh() -> None:
    if not DATA_DIR:
        _refresh.update(last_utc=now(), ok=False, log="DATA_DIR not set: refresh skipped")
        return
    if not _refresh_lock.acquire(blocking=False):
        return
    _refresh["running"] = True
    try:
        log = []
        for cmd in ([PY, "fetch_signals.py", "signals_raw.csv"],
                    [PY, "-W", "ignore", "backtest_signals.py", "signals_raw.csv", DATA_DIR]):
            r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=900)
            log.append(f"$ {' '.join(cmd[1:])}\n{r.stdout[-1500:]}{r.stderr[-800:]}")
            if r.returncode != 0:
                _refresh.update(ok=False, log="\n".join(log))
                return
        _refresh.update(ok=True, log="\n".join(log))
    except Exception as e:
        _refresh.update(ok=False, log=str(e))
    finally:
        _refresh.update(running=False, last_utc=now())
        _refresh_lock.release()


def refresher() -> None:
    while True:
        run_refresh()
        time.sleep(REFRESH_S)


def state() -> dict:
    seen = read_json(STATE / "seen.json", {})
    active = [{"id": k, **v} for k, v in seen.items() if v.get("status") == "active"]
    return {
        "server_utc": now(),
        "summary": read_json(RESULTS / "summary.json", None),
        "watcher": read_json(STATE / "status.json", None),
        "active": active,
        "events": tail_events(),
        "refresh": {k: v for k, v in _refresh.items()},
    }


class H(BaseHTTPRequestHandler):
    server_version = "tickmind-ui"

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write(f"[{now()}] {self.address_string()} {fmt % args}\n")

    def _authed(self) -> bool:
        if not (USER and PASSWORD):
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                if hmac.compare_digest(u, USER) and hmac.compare_digest(p, PASSWORD):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="tickmind"')
        self.end_headers()
        return False

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authed():
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

    def do_POST(self):
        if not self._authed():
            return
        if self.path == "/api/refresh":
            threading.Thread(target=run_refresh, daemon=True).start()
            self._send(202, b'{"started":true}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    host, port = os.environ.get("UI_HOST", "127.0.0.1"), int(os.environ.get("UI_PORT", "8077"))
    if os.environ.get("NO_REFRESH") != "1":
        threading.Thread(target=refresher, daemon=True).start()
    print(f"tickmind UI on http://{host}:{port}  (auth {'on' if USER and PASSWORD else 'OFF'})", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    main()
