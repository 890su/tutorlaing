from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .storage import Storage


def start_health_server(
    storage: Storage,
    host: str,
    port: int,
    webhook_handler: Callable[[dict[str, Any]], None] | None = None,
    webhook_secret: str = "",
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in {"/health", "/healthz"}:
                self.send_response(404)
                self.end_headers()
                return
            try:
                payload: dict[str, Any] = {"status": "ok", **storage.health()}
                status = 200
            except Exception as exc:
                payload = {"status": "error", "error": type(exc).__name__}
                status = 503
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/telegram/webhook" or webhook_handler is None:
                self.send_response(404)
                self.end_headers()
                return
            provided_secret = self.headers.get(
                "X-Telegram-Bot-Api-Secret-Token", ""
            )
            if not webhook_secret or not hmac.compare_digest(
                provided_secret, webhook_secret
            ):
                self.send_response(403)
                self.end_headers()
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if content_length <= 0 or content_length > 1_000_000:
                self.send_response(400)
                self.end_headers()
                return
            try:
                update = json.loads(self.rfile.read(content_length).decode("utf-8"))
                webhook_handler(update)
            except (json.JSONDecodeError, UnicodeDecodeError, KeyError, ValueError):
                self.send_response(400)
                self.end_headers()
                return
            except Exception:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
