"""Persistent local inbox dashboard, Gmail OAuth, and account-scoped JSON API."""

from __future__ import annotations

import argparse
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import sys
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .classification import ClassificationService
from .gmail import GmailError, GmailAuthError
from .gmail_auth import GmailAuthService, GmailSettings
from .jev import JevError, from_environment
from .mailbox import MailboxService
from .store import InboxStore, account_public
from .triage import CATEGORIES, PRIORITIES

MAX_EMAIL_BYTES = 25 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024
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

    def __init__(self, address: tuple[str, int], store: InboxStore, classifier: Any = None,
                 *, enable_mail: bool = False, auth: Any = None, mailbox: Any = None):
        self.store = store
        self.classification: ClassificationService | None = None
        self.mailbox: MailboxService | None = None
        super().__init__(address, InboxHandler)
        try:
            # Use a canonical configured callback, never an untrusted Host header.
            callback_host = address[0] if address[0] in {"localhost", "127.0.0.1"} else "127.0.0.1"
            callback = f"http://{callback_host}:{self.server_address[1]}/auth/gmail/callback"
            settings = (GmailSettings.from_environment(callback) if enable_mail else
                        GmailSettings(redirect_uri=callback))
            self.auth = auth if auth is not None else GmailAuthService(store, settings)
            bound_port = self.server_address[1]
            local_hosts = {"127.0.0.1", "localhost", address[0]}
            if address[0] == "0.0.0.0":
                try:
                    local_hosts.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
                except OSError:
                    pass
            self.allowed_hosts = {f"{name}:{bound_port}".lower() for name in local_hosts}
            if bound_port == 80:
                self.allowed_hosts.update(name.lower() for name in local_hosts)
            redirect = urlsplit(self.auth.settings.redirect_uri)
            self.allowed_hosts.add(redirect.netloc.lower())
            if redirect.port is None:
                self.allowed_hosts.add(f"{redirect.hostname}:{443 if redirect.scheme == 'https' else 80}".lower())
            self.classification = ClassificationService(store, classifier)
            self.mailbox = mailbox if mailbox is not None else MailboxService(
                store, self.auth, on_messages=self.classification.enqueue,
                enabled=enable_mail or auth is not None,
            )
        except Exception:
            if self.mailbox is not None:
                self.mailbox.close()
            if self.classification is not None:
                self.classification.close()
            super().server_close()
            raise

    def server_close(self) -> None:
        if self.mailbox is not None:
            self.mailbox.close()
        if self.classification is not None:
            self.classification.close()
        super().server_close()


class InboxHandler(BaseHTTPRequestHandler):
    server: InboxServer
    server_version = "InboxAtlas/0.4"
    sys_version = ""

    def _respond(self, status: int, body: bytes, content_type: str,
                 headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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

    def _redirect(self, location: str, *, clear_cookie: bool = False) -> None:
        headers = {"Location": location}
        if clear_cookie:
            headers["Set-Cookie"] = self._oauth_cookie("", clear=True)
        self._respond(303, b"", "text/plain; charset=utf-8", headers)

    def _oauth_cookie(self, nonce: str, *, clear: bool = False) -> str:
        secure = "; Secure" if urlsplit(self.server.auth.settings.redirect_uri).scheme == "https" else ""
        return (f"inbox_oauth={nonce}; HttpOnly; SameSite=Lax; Path=/auth/gmail/callback; "
                f"Max-Age={0 if clear else 600}{secure}")

    @staticmethod
    def _params(query: str, allowed: set[str]) -> dict[str, str]:
        params = parse_qs(query, keep_blank_values=True, max_num_fields=20)
        if set(params) - allowed or any(len(values) != 1 for values in params.values()):
            raise RequestError(400, "Unknown or repeated request parameter.")
        return {name: values[0] for name, values in params.items()}

    def _account(self, params: dict[str, str]) -> dict:
        account_id = params.pop("account", self.server.store.get_active_account_id())
        account = self.server.store.get_account(account_id)
        if account is None:
            raise RequestError(404, "Mailbox not found.")
        return account

    def _oauth_callback(self, query: str) -> None:
        # All failures use fixed codes. Never reflect Google codes or error text.
        result = "error"
        try:
            params = self._params(query, {"state", "code", "error", "error_description", "scope", "authuser", "prompt", "hd"})
            if "error" in params:
                result = "denied"
            elif params.get("state") and params.get("code"):
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                nonce = cookie.get("inbox_oauth")
                if nonce is None or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce.value):
                    raise RequestError(400, "The Gmail sign-in session expired. Try connecting again.")
                account = self.server.auth.complete(params["state"], params["code"], nonce.value)
                self.server.store.set_active_account_id(account["id"])
                self.server.mailbox.enqueue(account["id"])
                self._redirect("/?" + urlencode({"gmail": "connected", "account": account["id"]}), clear_cookie=True)
                return
        except (RequestError, GmailError, CookieError, ValueError, TypeError):
            pass
        except Exception:
            # Transport errors may carry secrets. Do not log their repr/traceback.
            logging.error("Gmail sign-in could not be completed")
        self._redirect("/?" + urlencode({"gmail": result}), clear_cookie=True)

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

    def _check_host(self) -> None:
        # Comparing Origin with arbitrary Host alone permits DNS rebinding to a
        # local app. Trust only our listening addresses and configured callback.
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in self.server.allowed_hosts:
            raise RequestError(403, "Use the app's local address or configured Gmail callback origin.")

    def _get(self, path: str, query: str) -> None:
        if path == "/auth/gmail/callback":
            if self.command != "GET":
                raise RequestError(405, "Gmail sign-in callbacks require GET.")
            self._oauth_callback(query)
        elif path in ASSETS:
            filename, content_type = ASSETS[path]
            asset = files("email_parser").joinpath("static", filename).read_bytes()
            self._respond(200, asset, content_type)
        elif path == "/api/inbox":
            filters = self._params(query, {"search", "category", "priority", "view", "account"})
            account = self._account(filters)
            account_id = account["id"]
            if filters.get("category") and filters["category"] not in {c["id"] for c in CATEGORIES}:
                raise RequestError(400, "Unknown category.")
            if filters.get("priority") and filters["priority"] not in PRIORITIES:
                raise RequestError(400, "Unknown priority.")
            if filters.get("view", "inbox") not in {"inbox", "unread", "starred", "archived"}:
                raise RequestError(400, "Unknown inbox view.")
            if len(filters.get("search", "")) > 500:
                raise RequestError(400, "Search must be 500 characters or fewer.")
            self._json(200, {
                "account": account_public(account),
                "categories": CATEGORIES,
                "priorities": PRIORITIES,
                "summary": self.server.store.summary(account_id),
                "messages": self.server.store.list_messages(**filters, account_id=account_id),
                "classifier": self.server.classification.status(account_id),
                "mailbox": self.server.mailbox.status(account_id),
                "preferences": self.server.store.get_preferences(account_id),
            })
        elif path == "/api/accounts":
            self._params(query, set())
            self._json(200, {
                "accounts": self.server.store.list_accounts(),
                "active_account_id": self.server.store.get_active_account_id(),
                "configuration": self.server.auth.configuration(),
            })
        elif path == "/api/preferences":
            account = self._account(self._params(query, {"account"}))
            self._json(200, self.server.store.get_preferences(account["id"]))
        elif path.startswith("/api/messages/") and "/" not in path[len("/api/messages/"):]:
            account = self._account(self._params(query, {"account"}))
            message = self.server.store.get_message(path[len("/api/messages/"):], account_id=account["id"])
            if message is None:
                raise RequestError(404, "Message not found.")
            self._json(200, message)
        else:
            raise RequestError(404, "Not found.")

    def _mutate(self, path: str, query: str) -> None:
        self._check_origin()
        if self.command == "PATCH" and path.startswith("/api/messages/"):
            account = self._account(self._params(query, {"account"}))
            changes = self._json_body()
            message = self.server.store.update_message(path[len("/api/messages/"):], changes, account_id=account["id"])
            if message is None:
                raise RequestError(404, "Message not found.")
            self._json(200, message)
        elif self.command == "PATCH" and path == "/api/preferences":
            account = self._account(self._params(query, {"account"}))
            self._json(200, self.server.store.set_preferences(account["id"], self._json_body()))
        elif self.command == "POST" and path == "/api/accounts/select":
            self._params(query, set())
            data = self._json_body()
            if set(data) != {"account_id"} or not isinstance(data["account_id"], str):
                raise RequestError(400, "Supply a mailbox account_id.")
            if self.server.store.get_account(data["account_id"]) is None:
                raise RequestError(404, "Mailbox not found.")
            self.server.store.set_active_account_id(data["account_id"])
            self._json(200, {"active_account_id": data["account_id"]})
        elif self.command == "POST" and path == "/api/accounts/gmail/start":
            self._params(query, set())
            data = self._json_body(allow_empty=True)
            if set(data) - {"account_id"} or ("account_id" in data and not isinstance(data["account_id"], str)):
                raise RequestError(400, "Supply an optional mailbox account_id for reconnecting.")
            if not self.server.auth.configuration()["configured"]:
                raise RequestError(409, "Gmail sign-in needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the server environment settings.")
            configured = urlsplit(self.server.auth.settings.redirect_uri)
            canonical_origin = f"{configured.scheme}://{configured.netloc}"
            browser_origin = self.headers.get("Origin", f"{configured.scheme}://{self.headers.get('Host', '')}")
            if browser_origin.lower().rstrip("/") != canonical_origin.lower():
                raise RequestError(409, f"Open Inbox Atlas at {canonical_origin} before connecting Gmail so the sign-in session can return to this browser.")
            nonce = secrets.token_urlsafe(32)
            result = self.server.auth.begin(nonce, expected_account_id=data.get("account_id"))
            body = json.dumps({"authorization_url": result["authorization_url"]}).encode("utf-8")
            self._respond(200, body, "application/json; charset=utf-8", {"Set-Cookie": self._oauth_cookie(nonce)})
        elif self.command == "POST" and re.fullmatch(r"/api/accounts/[^/]+/(sync|disconnect)", path):
            self._params(query, set())
            if self._json_body(allow_empty=True):
                raise RequestError(400, "This action does not accept parameters.")
            account_id, action = path.split("/")[3:]
            account = self.server.store.get_account(account_id)
            if account is None or account["provider"] != "gmail":
                raise RequestError(404, "Gmail mailbox not found.")
            if action == "sync":
                if not account["connected"]:
                    raise RequestError(409, "Reconnect this Gmail mailbox before syncing.")
                self._json(200, {"sync": self.server.mailbox.enqueue(account_id)})
            else:
                self._json(200, {"account": self.server.auth.disconnect(account_id)})
        elif self.command == "POST" and path in {"/api/demo/sync", "/api/demo/reset"}:
            self._params(query, set())
            if self._json_body(allow_empty=True):
                raise RequestError(400, "This action does not accept parameters.")
            if path.endswith("/sync"):
                result = self.server.store.sync_demo()
                self.server.classification.enqueue()
                self._json(200, result)
            else:
                self.server.store.reset_demo()
                self.server.classification.enqueue()
                self._json(200, {"reset": True})
        elif self.command == "POST" and path in {"/api/classification/retry", "/api/classification/run"}:
            self._account(self._params(query, {"account"}))
            if self._json_body(allow_empty=True):
                raise RequestError(400, "This action does not accept parameters.")
            if not self.server.classification.status()["configured"]:
                raise RequestError(409, "Jev is not configured. Add the server's TypeSafe API key in environment settings.")
            result = (
                self.server.classification.retry()
                if path.endswith("/retry")
                else self.server.classification.enqueue(force=True)
            )
            self._json(200, result)
        elif self.command == "POST" and path == "/api/import":
            account = self._account(self._params(query, {"account"}))
            if self.headers.get_content_type() not in {"message/rfc822", "application/octet-stream"}:
                raise RequestError(415, "Send a raw .eml file as message/rfc822 or application/octet-stream.")
            raw = self._read_body(MAX_EMAIL_BYTES)
            if not raw.strip():
                raise RequestError(400, "The email file is empty.")
            message = self.server.store.import_email(raw, account_id=account["id"])
            self.server.classification.enqueue()
            self._json(200, {"message": self.server.store.get_message(message["id"], account_id=account["id"])})
        else:
            raise RequestError(404, "Not found.")

    def _handle(self) -> None:
        try:
            self._check_host()
            url = urlsplit(self.path)
            if url.scheme or url.netloc or not url.path.startswith("/"):
                raise RequestError(400, "Invalid request path.")
            if self.command in {"GET", "HEAD"}:
                self._get(url.path, url.query)
            elif self.command in {"POST", "PATCH"}:
                self._mutate(url.path, url.query)
            else:
                raise RequestError(405, "Method not allowed.")
        except RequestError as error:
            self._json(error.status, {"error": str(error)})
        except GmailError as error:
            self._json(409 if isinstance(error, GmailAuthError) else 502, {"error": str(error)})
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


def create_server(
    host: str, port: int, db_path: str | Path, *, enable_ai: bool = False, classifier: Any = None,
    enable_mail: bool = False, auth: Any = None, mailbox: Any = None,
) -> InboxServer:
    """Bind a server, with external integrations opt-in for embedding/tests.

    The command-line app enables Jev from server environment settings. Tests can
    inject a fake classifier; ordinary factory use never discovers credentials.
    Callers own serve_forever(), shutdown(), and server_close().
    """
    store = InboxStore(db_path)
    client = classifier if classifier is not None else from_environment() if enable_ai else None
    try:
        return InboxServer((host, port), store, client, enable_mail=enable_mail, auth=auth, mailbox=mailbox)
    except Exception:
        if mailbox is not None:
            try:
                mailbox.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        raise


def default_database_path() -> Path:
    """A stable data location independent of the shell's current directory."""
    if os.environ.get("INBOX_DATA_DIR", "").strip():
        return Path(os.environ["INBOX_DATA_DIR"]).expanduser().resolve() / "inbox.sqlite3"
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file() and (checkout / "src" / "email_parser").is_dir():
        return checkout / "output" / "inbox.sqlite3"
    if os.name == "nt":
        data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        data = Path.home() / "Library" / "Application Support"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data / "inbox-atlas" / "inbox.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Inbox Atlas with persistent Gmail and demo mailboxes.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    parser.add_argument("--db", type=Path, default=default_database_path(), help="SQLite inbox file (defaults to a stable app data directory)")
    parser.add_argument("--no-ai", action="store_true", help="show preview labels without making Jev API calls")
    parser.add_argument("--no-mail", action="store_true", help="disable Gmail authorization and automatic sync for this session")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        server = create_server(args.host, args.port, args.db, enable_ai=not args.no_ai, enable_mail=not args.no_mail)
    except (OSError, sqlite3.Error, JevError, GmailError, ValueError) as error:
        parser.exit(2, f"email-inbox: {error}\n")
    print(f"Inbox Atlas running on {server.server_address[0]}:{server.server_address[1]} (persistent mailboxes)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
