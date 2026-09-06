"""Loopback viewer with read-only evidence and separate manual records."""
from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mu_strategy.market_data.trusted_data.contracts import SystemClock
from mu_strategy.manual_positions import ManualPositionLedger
from mu_strategy.position_management import review_position
from mu_strategy.notifications.events import AlertKind, NotificationError
from mu_strategy.notifications.store import NotificationStore
from mu_strategy.signal_feedback import SignalFeedbackStore
from mu_strategy.signal_review import read_signal_review, review_window
from mu_strategy.viz.signal_review import REVIEW_STYLE, render_signal_review
from mu_strategy.viz.position_ledger import render_position_editor, render_position_management_editor, render_position_state_editor


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

        def respond(self, status: int, content: str, content_type="text/html; charset=utf-8", *, location=None):
            encoded = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if location is not None:
                self.send_header("Location", location)
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
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
            if urlsplit(self.path).path in {"/positions", "/position-state", "/position-management"}:
                self.position_editor()
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
            if self.path not in {"/feedback", "/positions", "/position-state", "/position-management"}:
                self.respond(405, "Unsupported endpoint")
                return
            host = self.headers.get("Host")
            if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"} or self.headers.get("Origin") != "http://" + host:
                self.respond(403, "Same-origin local access required")
                return
            if self.path in {"/positions", "/position-state", "/position-management"}:
                self.save_position()
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

        def position_editor(self):
            ledger = ManualPositionLedger(data_dir)
            state_editor = urlsplit(self.path).path == "/position-state"
            management_editor = urlsplit(self.path).path == "/position-management"
            try:
                query = parse_qs(urlsplit(self.path).query, max_num_fields=5)
                allowed = {"position_id"} if state_editor else {"position_id", "saved"} if management_editor else {"position_id", "fill_id", "event_id", "saved"}
                if set(query) - allowed or any(len(values) != 1 for values in query.values()):
                    raise ValueError()
                options = {key: values[0] for key, values in query.items()}
                view = ledger.view()
                position_id, fill_id = options.get("position_id"), options.get("fill_id")
                if (state_editor or management_editor) and not position_id:
                    raise ValueError()
                if view["available"] and (position_id or fill_id):
                    position = next((item for item in view["positions"] if item["position_id"] == position_id), None)
                    if position is None or (fill_id and not any(item["fill_id"] == fill_id for item in position["fills"])):
                        self.respond(404, "持仓或成交记录不存在。")
                        return
                if options.get("event_id") and (position_id or fill_id):
                    raise ValueError()
                if management_editor:
                    review = review_position(position, data_dir, now_ms=clock.now_ms()) if view["available"] else None
                    content = render_position_management_editor(view, stylesheet=REVIEW_STYLE, position_id=position_id,
                                                                 review=review, saved=options.get("saved") == "1")
                elif state_editor:
                    content = render_position_state_editor(view, stylesheet=REVIEW_STYLE, position_id=position_id)
                else:
                    source = ledger.entry_source(options["event_id"]) if options.get("event_id") else None
                    saved = "state" if options.get("saved") == "state" else options.get("saved") == "1"
                    content = render_position_editor(view, stylesheet=REVIEW_STYLE, position_id=position_id,
                                                      fill_id=fill_id, source=source, saved=saved)
            except (ValueError, UnicodeError):
                self.respond(400, "录入入口无效，请从复盘页重新打开。")
                return
            except (OSError, sqlite3.Error, NotificationError):
                self.respond(503, "关联来源暂不可用，请稍后重试。")
                return
            self.respond(200 if view["available"] else 503, content)

        def save_position(self):
            ledger, payload = ManualPositionLedger(data_dir), {}
            state_editor = self.path == "/position-state"
            management_editor = self.path == "/position-management"
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    raise ValueError("录入请求格式无效。")
                fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True,
                                  max_num_fields=64 if management_editor else 24)
                if any(len(values) != 1 for values in fields.values()):
                    raise ValueError("录入字段重复。")
                payload = {key: values[0] for key, values in fields.items()}
                save = ledger.save_management if management_editor else ledger.save_state if state_editor else ledger.save
                position_id = save(payload, now_ms=clock.now_ms())
            except (ValueError, UnicodeError) as exc:
                status, error = 400, str(exc)
            except (OSError, sqlite3.Error, NotificationError):
                status, error = 503, "管理输入暂时无法保存，请稍后重试。" if management_editor else "持仓状态暂时无法保存，请稍后重试。" if state_editor else "成交记录暂时无法保存，请稍后重试。"
            else:
                location = (f"/position-management?position_id={position_id}&saved=1#position-review" if management_editor else
                            f"/positions?saved={'state' if state_editor else '1'}#position-{position_id}")
                self.respond(303, "", location=location)
                return
            if management_editor:
                content = render_position_management_editor(ledger.view(), stylesheet=REVIEW_STYLE, draft=payload,
                                                             position_id=payload.get("position_id"), error=error)
            elif state_editor:
                content = render_position_state_editor(ledger.view(), stylesheet=REVIEW_STYLE, draft=payload,
                                                        position_id=payload.get("position_id"), error=error)
            else:
                content = render_position_editor(ledger.view(), stylesheet=REVIEW_STYLE, draft=payload,
                                                  position_id=payload.get("position_id"), fill_id=payload.get("fill_id"), error=error)
            self.respond(status, content)

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
