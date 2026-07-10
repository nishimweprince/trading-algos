from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Callable


def start_health_server(host: str, port: int, status_provider: Callable[[], dict]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._send({"status": "ok"})
                return
            if self.path == "/status":
                self._send(status_provider())
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            return

        def _send(self, body: dict) -> None:
            payload = json.dumps(body, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server

