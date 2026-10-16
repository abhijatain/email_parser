"""Local inbox dashboard and JSON API, backed by a simulated mail provider."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .store import InboxStore
from .triage import CATEGORIES, PRIORITIES

MAX_EMAIL_BYTES = 25 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024
ACCOUNT = {
    "name": "Alex Morgan",
    "email": "alex.morgan@example.com",
    "provider": "Simulated mailbox",
    "mode": "demo",
    "connected": False,
}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class InboxServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: InboxStore):
        self.store = store
        super().__init__(address, InboxHandler)


class InboxHandler(BaseHTTPRequestHandler):
    server: InboxServer
    server_version = "InboxAtlas/0.2"
    sys_version = ""

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        # Close after a response, including rejected requests with unread bodies.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8")

    def _read_body(self, limit: int) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "Transfer-Encoding is not supported; send Content-Length.")
        lengths = self.headers.get_all("Content-Length", [])
        raw_length = lengths[0] if lengths else "0"
        if len(lengths) > 1 or not re.fullmatch(r"[0-9]+", raw_length):
            raise RequestError(400, "Invalid Content-Length.")
        length = int(raw_length)
        if length > limit:
            raise RequestError(413, f"Request exceeds the {limit}-byte limit.")
        self.connection.settimeout(15)
        try:
            body = self.rfile.read(length)
        except TimeoutError as error:
            raise RequestError(408, "Timed out reading the request body.") from error
        if len(body) != length:
            raise RequestError(400, "Incomplete request body.")
        return body

    def _json_body(self, allow_empty: bool = False) -> dict:
        raw = self._read_body(MAX_JSON_BYTES)
        if allow_empty and not raw:
            return {}
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise RequestError(415, "Send JSON with Content-Type: application/json.")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise RequestError(400, "Invalid JSON body.") from error
        if not isinstance(value, dict):
            raise RequestError(400, "The JSON body must be an object.")
        return value

    def _check_origin(self) -> None:
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError(403, "Cross-origin changes are not allowed.")
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urlsplit(origin)
            except ValueError as error:
                raise RequestError(403, "Invalid request origin.") from error
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.netloc.lower() != self.headers.get("Host", "").lower()
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise RequestError(403, "Cross-origin changes are not allowed.")

    def _get(self, path: str, query: str) -> None:
        if path in ASSETS:
            filename, content_type = ASSETS[path]
            asset = files("email_parser").joinpath("static", filename).read_bytes()
            self._respond(200, asset, content_type)
        elif path == "/api/inbox":
            params = parse_qs(query, keep_blank_values=True, max_num_fields=20)
            allowed = {"search", "category", "priority", "view"}
            if set(params) - allowed or any(len(values) != 1 for values in params.values()):
                raise RequestError(400, "Unknown or repeated inbox filter.")
            filters = {name: values[0] for name, values in params.items()}
            if filters.get("category") and filters["category"] not in {c["id"] for c in CATEGORIES}:
                raise RequestError(400, "Unknown category.")
            if filters.get("priority") and filters["priority"] not in PRIORITIES:
                raise RequestError(400, "Unknown priority.")
            if filters.get("view", "inbox") not in {"inbox", "unread", "starred", "archived"}:
                raise RequestError(400, "Unknown inbox view.")
            if len(filters.get("search", "")) > 500:
                raise RequestError(400, "Search must be 500 characters or fewer.")
            self._json(200, {
                "account": ACCOUNT,
                "categories": CATEGORIES,
                "priorities": PRIORITIES,
                "summary": self.server.store.summary(),
                "messages": self.server.store.list_messages(**filters),
            })
        elif path.startswith("/api/messages/") and "/" not in path[len("/api/messages/"):]:
            message = self.server.store.get_message(path[len("/api/messages/"):])
            if message is None:
                raise RequestError(404, "Message not found.")
            self._json(200, message)
        else:
            raise RequestError(404, "Not found.")

    def _mutate(self, path: str) -> None:
        self._check_origin()
        if self.command == "PATCH" and path.startswith("/api/messages/"):
            changes = self._json_body()
            message = self.server.store.update_message(path[len("/api/messages/"):], changes)
            if message is None:
                raise RequestError(404, "Message not found.")
            self._json(200, message)
        elif self.command == "POST" and path in {"/api/demo/sync", "/api/demo/reset"}:
            if self._json_body(allow_empty=True):
                raise RequestError(400, "This action does not accept parameters.")
            if path.endswith("/sync"):
                self._json(200, self.server.store.sync_demo())
            else:
                self.server.store.reset_demo()
                self._json(200, {"reset": True})
        elif self.command == "POST" and path == "/api/import":
            if self.headers.get_content_type() not in {"message/rfc822", "application/octet-stream"}:
                raise RequestError(415, "Send a raw .eml file as message/rfc822 or application/octet-stream.")
            raw = self._read_body(MAX_EMAIL_BYTES)
            if not raw.strip():
                raise RequestError(400, "The email file is empty.")
            self._json(200, {"message": self.server.store.import_email(raw)})
        else:
            raise RequestError(404, "Not found.")

    def _handle(self) -> None:
        try:
            url = urlsplit(self.path)
            if url.scheme or url.netloc or not url.path.startswith("/"):
                raise RequestError(400, "Invalid request path.")
            if self.command in {"GET", "HEAD"}:
                self._get(url.path, url.query)
            elif self.command in {"POST", "PATCH"}:
                self._mutate(url.path)
            else:
                raise RequestError(405, "Method not allowed.")
        except RequestError as error:
            self._json(error.status, {"error": str(error)})
        except (ValueError, TypeError) as error:
            self._json(400, {"error": str(error)})
        except Exception:
            logging.exception("Inbox request failed")
            self._json(500, {"error": "The inbox could not complete this request."})

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PATCH = _handle
    do_PUT = _handle
    do_DELETE = _handle

    def log_message(self, format: str, *args: Any) -> None:
        # Paths may contain private search terms; log status without URLs or bodies.
        logging.info("%s request from %s", self.command, self.client_address[0])


def create_server(host: str, port: int, db_path: str | Path) -> InboxServer:
    """Bind a server; callers own serve_forever(), shutdown(), and server_close()."""
    return InboxServer((host, port), InboxStore(db_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Inbox Atlas with a persistent simulated mailbox.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    parser.add_argument("--db", type=Path, default=Path("output/inbox.sqlite3"), help="SQLite inbox file")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        server = create_server(args.host, args.port, args.db)
    except (OSError, sqlite3.Error) as error:
        parser.exit(2, f"email-inbox: {error}\n")
    print(f"Inbox Atlas running on {server.server_address[0]}:{server.server_address[1]} (demo mailbox)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
