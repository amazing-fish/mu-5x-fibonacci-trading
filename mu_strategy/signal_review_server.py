"""Loopback viewer with read-only evidence and separate personal annotations."""
from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mu_strategy.market_data.trusted_data.contracts import SystemClock
from mu_strategy.notifications.events import AlertKind, NotificationError
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.signal_feedback import SignalFeedbackStore
from mu_strategy.signal_review import read_signal_review, review_window
from mu_strategy.viz.signal_review import render_signal_review


def make_review_server(data_dir: Path, *, port: int = 8769, days: int = 7,
                       from_date: str | None = None, to_date: str | None = None,
                       clock=None) -> HTTPServer:
    clock = clock or SystemClock()
    options = {"days": days, "from_date": from_date, "to_date": to_date}
    review_window(now_ms=clock.now_ms(), **options)
    data_dir = Path(data_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def respond(self, status: int, content: str, content_type="text/html; charset=utf-8"):
            encoded = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            if self.headers.get("Host") not in hosts or (origin is not None and origin not in {"http://" + host for host in hosts}):
                self.respond(403, "Local access only")
                return
            if self.path not in {"/", "/report"}:
                self.respond(404, "Not found")
                return
            try:
                # Resolve the default date window anew, including after midnight.
                window = review_window(now_ms=clock.now_ms(), **options)
                report = read_signal_review(data_dir, window, clock=clock)
                content = render_signal_review(report, live=True)
            except Exception:
                self.respond(503, "Report is temporarily unavailable")
                return
            self.respond(200, content)

        def do_POST(self):
            if self.path != "/feedback":
                self.respond(405, "Unsupported endpoint")
                return
            host = self.headers.get("Host")
            if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"} or self.headers.get("Origin") != "http://" + host:
                self.respond(403, "Same-origin local access required")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384 or self.headers.get_content_type() != "application/json":
                    raise ValueError("invalid request")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("invalid request")
                event_id = payload.get("event_id")
                if not isinstance(event_id, str):
                    raise ValueError("invalid event id")
                store = NotificationStore(data_dir)
                with store.connection(readonly=True) as db:
                    record = store.record(db, event_id)
                    if record is None or record.event.kind is not AlertKind.ENTRY_REVIEW:
                        raise ValueError("feedback requires an entry review")
                saved = SignalFeedbackStore(data_dir).save(event_id, payload.get("status"), payload.get("note"), now_ms=clock.now_ms())
            except (ValueError, UnicodeError):
                self.respond(400, "Invalid feedback")
                return
            except (OSError, sqlite3.Error, NotificationError):
                self.respond(503, "Feedback could not be saved")
                return
            self.respond(200, json.dumps(saved, ensure_ascii=False), "application/json; charset=utf-8")

        def do_PUT(self):
            self.respond(405, "Unsupported method")

        do_DELETE = do_PATCH = do_PUT

    return HTTPServer(("127.0.0.1", port), Handler)


def serve_signal_review(data_dir: Path, *, stdout, **options) -> int:
    try:
        server = make_review_server(data_dir, **options)
    except OSError:
        stdout.write(json.dumps({"error_code": "review_server_bind_failed"}) + "\n")
        return 2
    stdout.write(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/", "mode": "live_review"}) + "\n")
    stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
