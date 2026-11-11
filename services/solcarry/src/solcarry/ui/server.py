"""Tiny stdlib HTTP server: the dashboard page plus read-only JSON/PNG/MD from results/."""

from __future__ import annotations

import json
import mimetypes
import re
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

RUN_RE = re.compile(r"^[0-9A-Za-z_.-]+$")
ALLOWED = {".json", ".png", ".md", ".csv"}


def list_runs(results: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    if not results.exists():
        return runs
    for d in sorted((p for p in results.iterdir() if p.is_dir()), reverse=True):
        g = d / "gate.json"
        info: dict[str, Any] = {"run_id": d.name, "has_ui": (d / "ui.json").exists()}
        if g.exists():
            try:
                gj = json.loads(g.read_text())
                info["result"] = gj.get("result")
                info["unlocked"] = gj.get("unlocked")
            except json.JSONDecodeError:
                pass
        runs.append(info)
    return runs


class Handler(BaseHTTPRequestHandler):
    results: Path

    def __init__(self, *a: Any, results: Path, **kw: Any) -> None:
        self.results = results
        super().__init__(*a, **kw)

    def log_message(self, *args: Any) -> None:  # quiet
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = resources.files("solcarry.ui").joinpath("static/index.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/runs":
            runs = [r for r in list_runs(self.results) if r["has_ui"]]
            return self._send(200, json.dumps(runs).encode(), "application/json")
        m = re.match(r"^/runs/([^/]+)/([^/]+)$", path)
        if m and RUN_RE.match(m.group(1)) and RUN_RE.match(m.group(2)):
            f = (self.results / m.group(1) / m.group(2)).resolve()
            if f.suffix in ALLOWED and f.is_file() and self.results.resolve() in f.parents:
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if f.suffix == ".md":
                    ctype = "text/markdown; charset=utf-8"
                return self._send(200, f.read_bytes(), ctype)
        return self._send(404, b"not found", "text/plain")


def make_server(results: Path, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), partial(Handler, results=results))


def serve(results: Path, host: str = "127.0.0.1", port: int = 8765) -> None:  # pragma: no cover
    srv = make_server(results, host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
