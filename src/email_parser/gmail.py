"""Small read-only Gmail REST adapter with bounded, verified HTTPS requests.

The injectable transport accepts ``(method, url, *, headers, body, timeout,
max_bytes)`` and returns ``(status, headers, response_bytes)``. Neither provider
response bodies nor transport exception text are exposed in application errors.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
_JSON_LIMIT = 2 * 1024 * 1024
_RAW_JSON_LIMIT = ((MAX_MESSAGE_BYTES + 2) // 3) * 4 + _JSON_LIMIT
_ID = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")


class GmailError(RuntimeError):
    """An intentionally sanitized provider error safe to show in the inbox."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class GmailAuthError(GmailError):
    """A mailbox needs authorization or a corrected server configuration."""


class GmailHistoryExpired(GmailError):
    """Gmail no longer retains the requested incremental history cursor."""


class GmailMessageMissing(GmailError):
    """A message was deleted between listing and downloading it."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # In particular, never forward bearer tokens or OAuth form data to a
        # destination selected by a redirect response.
        return None


def https_request(method: str, url: str, *, headers: dict, body: bytes | None,
                  timeout: float, max_bytes: int) -> tuple[int, dict, bytes]:
    """Use the platform's normal CA verification; allow only Google API hosts."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"gmail.googleapis.com", "oauth2.googleapis.com"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise GmailError("Invalid Google API destination.")
    request = Request(url, data=body, headers=headers, method=method)
    try:
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise GmailError("Google returned a response larger than the supported limit.")
            return response.code, dict(response.headers), data
    except GmailError:
        raise
    except (URLError, OSError, ValueError):
        raise GmailError("Could not reach Google securely. Check the connection and try again.") from None


def request_json(request, method: str, url: str, *, headers: dict | None = None,
                 body: bytes | None = None, timeout: float = 15,
                 max_bytes: int = _JSON_LIMIT, history: bool = False,
                 message: bool = False, oauth: bool = False) -> dict:
    """Shared protocol handling for the REST client and token exchange."""
    try:
        status, _headers, data = request(method, url, headers=headers or {}, body=body,
                                         timeout=timeout, max_bytes=max_bytes)
    except GmailError:
        raise
    except Exception:
        raise GmailError("Could not reach Google securely. Check the connection and try again.") from None
    if type(status) is not int or not isinstance(data, bytes) or len(data) > max_bytes:
        raise GmailError("Google returned an invalid or oversized response.")
    if status == 404 and history:
        raise GmailHistoryExpired("Gmail history expired. Refreshing the mailbox history.", status=status)
    if status == 404 and message:
        raise GmailMessageMissing("This Gmail message is no longer available.", status=status)
    if status in {401, 403} or (oauth and status == 400):
        raise GmailAuthError("Google authorization failed. Reconnect Gmail and grant read-only mail access.", status=status)
    if status == 429:
        raise GmailError("Gmail rate limit reached. Wait before syncing again.", status=status)
    if status >= 500:
        raise GmailError("Google is temporarily unavailable. Try syncing again later.", status=status)
    if not 200 <= status < 300:
        raise GmailError("Google rejected the request. Check the account connection and try again.", status=status)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise GmailError("Google returned an invalid response. Try again later.") from None
    if not isinstance(value, dict):
        raise GmailError("Google returned an invalid response. Try again later.")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise GmailError("Google returned an invalid message or history identifier.")
    return value


def _page(value: str | None) -> dict:
    if value is None:
        return {}
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise GmailError("Google returned an invalid pagination token.")
    return {"pageToken": value}


def _epoch(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise GmailError("Mailbox sync requires timezone-aware dates.")
    try:
        return math.floor(value.timestamp())
    except (ValueError, OverflowError, OSError):
        raise GmailError("Mailbox sync date is outside the supported range.") from None


class GmailClient:
    """Read Gmail messages using the authenticated user's ``me`` identity."""

    def __init__(self, access_token: str, *, request=None, timeout: float = 15):
        if (not isinstance(access_token, str) or not access_token or len(access_token) > 16384
                or any(ord(char) < 33 or ord(char) == 127 for char in access_token)):
            raise GmailAuthError("A valid Gmail access token is required. Reconnect Gmail.")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
            raise GmailError("Gmail timeout must be a positive number of seconds.")
        self._access_token = access_token
        self._request = request or https_request
        self._timeout = timeout

    def _get(self, path: str, parameters: dict | None = None, **options) -> dict:
        url = API_BASE + path
        if parameters:
            url += "?" + urlencode(parameters)
        return request_json(self._request, "GET", url,
                            headers={"Authorization": "Bearer " + self._access_token, "Accept": "application/json"},
                            timeout=self._timeout, **options)

    def profile(self) -> dict:
        result = self._get("/profile")
        email = result.get("emailAddress")
        if (not isinstance(email, str) or len(email) > 320 or email.count("@") != 1
                or any(char.isspace() or ord(char) < 32 for char in email)):
            raise GmailError("Google returned an invalid mailbox profile.")
        _identifier(result.get("historyId"))
        return result

    def list_messages(self, after: datetime, before: datetime | None = None,
                      page_token: str | None = None) -> dict:
        # Gmail date strings otherwise use its Pacific time zone. Overfetch one
        # second on both edges, then let the worker enforce the exact window
        # against authoritative millisecond internalDate values.
        query = "after:" + str(_epoch(after) - 1)
        if before is not None:
            if before < after:
                raise GmailError("Mailbox sync end date precedes its start date.")
            query += " before:" + str(_epoch(before) + 1)
        result = self._get("/messages", {"q": query, "includeSpamTrash": "false", "maxResults": 500, **_page(page_token)})
        messages = result.get("messages", [])
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
            raise GmailError("Google returned an invalid message listing.")
        for item in messages:
            _identifier(item.get("id"))
        if "nextPageToken" in result:
            _page(result["nextPageToken"])
        result["messages"] = messages
        return result

    def get_message(self, message_id: str) -> dict:
        message_id = _identifier(message_id)
        result = self._get("/messages/" + message_id, {"format": "raw"},
                           max_bytes=_RAW_JSON_LIMIT, message=True)
        if result.get("id") != message_id:
            raise GmailError("Google returned a different message than requested.")
        raw = result.get("raw")
        if not isinstance(raw, str) or len(raw) > ((MAX_MESSAGE_BYTES + 2) // 3) * 4:
            raise GmailError("This Gmail message exceeds the supported 25 MiB limit or is invalid.")
        try:
            encoded = raw.encode("ascii")
            # Gmail returns unpadded base64url. Reject noncanonical padding and
            # standard-base64 characters instead of silently accepting damage.
            if not re.fullmatch(rb"[A-Za-z0-9_-]*={0,2}", encoded):
                raise ValueError
            unpadded = encoded.rstrip(b"=")
            if len(unpadded) % 4 == 1 or (b"=" in encoded and len(encoded) % 4):
                raise ValueError
            content = base64.b64decode(unpadded + b"=" * (-len(unpadded) % 4), altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(content).rstrip(b"=") != unpadded:
                raise ValueError
        except (ValueError, UnicodeError, binascii.Error):
            raise GmailError("Google returned an invalid encoded message.") from None
        if len(content) > MAX_MESSAGE_BYTES:
            raise GmailError("This Gmail message exceeds the supported 25 MiB limit.")
        timestamp = result.get("internalDate")
        if not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{1,16}", timestamp):
            raise GmailError("Google returned an invalid message arrival time.")
        try:
            internal_date = datetime.fromtimestamp(int(timestamp) / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise GmailError("Google returned an invalid message arrival time.") from None
        labels = result.get("labelIds", [])
        if not isinstance(labels, list) or not all(isinstance(label, str) and len(label) <= 512 for label in labels):
            raise GmailError("Google returned invalid message labels.")
        thread_id = _identifier(result.get("threadId"))
        return {"id": message_id, "raw": content, "internal_date": internal_date,
                "labels": labels, "thread_id": thread_id}

    def history(self, history_id: str, page_token: str | None = None) -> dict:
        result = self._get("/history", {"startHistoryId": _identifier(history_id), "maxResults": 500,
                                       **_page(page_token)}, history=True)
        _identifier(result.get("historyId"))
        if "history" in result and not isinstance(result["history"], list):
            raise GmailError("Google returned invalid mailbox history.")
        if "nextPageToken" in result:
            _page(result["nextPageToken"])
        return result
