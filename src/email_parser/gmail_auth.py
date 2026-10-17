"""Local Gmail OAuth consent, browser-bound PKCE, and encrypted token storage."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .gmail import GmailAuthError, GmailClient, GmailError, READONLY_SCOPE, https_request, request_json


AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_STATE_SECONDS = 600
_REFRESH_MARGIN = 60


def _redirect_uri(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise GmailAuthError("Configure a valid Google OAuth redirect URI on the server.") from None
    if (not isinstance(value, str) or not value or len(value) > 2048
            or parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in value or "#" in value
            or any(char.isspace() or ord(char) < 32 for char in value)
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"})
            or port == 0):
        raise GmailAuthError("Google OAuth redirect URI must use HTTPS or HTTP on localhost/127.0.0.1, with no credentials, query, or fragment.")
    return value


@dataclass(frozen=True)
class GmailSettings:
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    redirect_uri: str = "http://127.0.0.1:8000/auth/gmail/callback"

    def __post_init__(self):
        _redirect_uri(self.redirect_uri)
        for value in (self.client_id, self.client_secret):
            if not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 32 for char in value):
                raise GmailAuthError("Configure valid Google OAuth client credentials on the server.")

    @property
    def configured(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())

    @classmethod
    def from_environment(cls, default_redirect_uri: str):
        def configured_name(*names):
            for name in names:
                value = os.environ.get(name, "").strip()
                if value:
                    return value
            return ""

        return cls(
            configured_name("GOOGLE_CLIENT_ID", "INBOX_GOOGLE_CLIENT_ID", "GMAIL_CLIENT_ID"),
            configured_name("GOOGLE_CLIENT_SECRET", "INBOX_GOOGLE_CLIENT_SECRET", "GMAIL_CLIENT_SECRET"),
            configured_name("INBOX_GOOGLE_REDIRECT_URI") or default_redirect_uri,
        )


class TokenVault:
    """Fernet encryption with one persistent local key; back it up with the DB."""

    def __init__(self, key_path: Path):
        self.key_path = Path(key_path)
        try:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                descriptor = os.open(self.key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as existing:
                    if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                        raise ValueError
                    if hasattr(os, "fchmod"):
                        os.fchmod(existing.fileno(), 0o600)
                    else:  # Windows exposes chmod but does not expose fchmod.
                        os.chmod(self.key_path, 0o600)
                    key = existing.read(1024)
            else:
                key = Fernet.generate_key()
                with os.fdopen(descriptor, "wb") as created:
                    created.write(key)
                    created.flush()
                    os.fsync(created.fileno())
            self._fernet = Fernet(key)
        except (OSError, ValueError, TypeError):
            raise GmailAuthError("Cannot read the local Gmail encryption key. Restore the original key and database together or choose a writable data directory.") from None

    def encrypt(self, value: dict) -> str:
        try:
            if not isinstance(value, dict):
                raise ValueError
            data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
            return self._fernet.encrypt(data).decode("ascii")
        except (ValueError, TypeError):
            raise GmailAuthError("Cannot encrypt the Gmail connection state.") from None

    def decrypt(self, value: str) -> dict:
        try:
            decoded = self._fernet.decrypt(value.encode("ascii"))
            result = json.loads(decoded)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (InvalidToken, ValueError, TypeError, UnicodeError, AttributeError):
            raise GmailAuthError("Cannot decrypt the Gmail connection. Restore its original key and database together, or reconnect the account.") from None


def _hash(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise GmailAuthError("This Gmail sign-in session is invalid. Start sign-in again.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 16384
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise GmailAuthError("Google returned invalid authorization credentials. Reconnect Gmail.")
    return value


def _tokens(result: dict, *, old_refresh: str | None = None) -> dict:
    access = _token(result.get("access_token"))
    refresh = result.get("refresh_token") or old_refresh
    if refresh is None:
        raise GmailAuthError("Google did not grant persistent access. Reconnect Gmail and approve offline read-only access.")
    refresh = _token(refresh)
    scope = result.get("scope")
    if scope is not None and (not isinstance(scope, str) or READONLY_SCOPE not in scope.split()):
        raise GmailAuthError("Google did not grant read-only Gmail access. Reconnect and approve the requested permission.")
    token_type = result.get("token_type", "Bearer")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise GmailAuthError("Google returned an unsupported authorization type.")
    lifetime = result.get("expires_in")
    if type(lifetime) not in {int, float} or not math.isfinite(lifetime) or lifetime <= 0:
        raise GmailAuthError("Google returned an invalid access token expiry. Reconnect Gmail.")
    expires_at = time.time() + lifetime
    if not math.isfinite(expires_at):
        raise GmailAuthError("Google returned an invalid access token expiry. Reconnect Gmail.")
    return {"access_token": access, "refresh_token": refresh,
            "expires_at": expires_at, "scope": scope or READONLY_SCOPE, "token_type": "Bearer"}


class GmailAuthService:
    """Keep OAuth and token refresh entirely on the local server."""

    def __init__(self, store, settings: GmailSettings, *, vault=None, request=None, client_factory=GmailClient):
        self.store = store
        self.settings = settings
        self._vault = vault
        self._vault_lock = threading.Lock()
        self._request = request or https_request
        self._client_factory = client_factory
        self._locks_guard = threading.Lock()
        self._refresh_locks: dict[str, threading.Lock] = {}

    def configuration(self) -> dict:
        missing = []
        if not self.settings.client_id.strip():
            missing.append("GOOGLE_CLIENT_ID")
        if not self.settings.client_secret.strip():
            missing.append("GOOGLE_CLIENT_SECRET")
        return {"configured": self.settings.configured, "redirect_uri": self.settings.redirect_uri, "missing": missing}

    def _get_vault(self) -> TokenVault:
        with self._vault_lock:
            if self._vault is None:
                key_path = self.store.db_path.parent / ".inbox-token-key"
                if not key_path.exists() and any(self.store.get_account_tokens(account["id"])
                                                for account in self.store.list_accounts()):
                    raise GmailAuthError("The Gmail encryption key is missing. Restore the original key with the database before using saved connections.")
                self._vault = TokenVault(key_path)
            return self._vault

    def _require_configuration(self):
        if not self.settings.configured:
            raise GmailAuthError("Configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET securely on the server before connecting Gmail.")

    def begin(self, browser_nonce: str, expected_account_id: str | None = None) -> dict:
        self._require_configuration()
        browser_hash = _hash(browser_nonce)
        expected = None
        if expected_account_id is not None:
            expected = self.store.get_account(expected_account_id)
            if expected is None or expected.get("provider") != "gmail":
                raise GmailAuthError("Select a Gmail account before reconnecting it.")
        # The user may choose any mailbox in Google's account chooser. Bind
        # every account already known at consent start to its generation, so
        # disconnect/reconnect wins even without an explicit reconnect target.
        generations = {}
        for public in self.store.list_accounts():
            if public.get("provider") == "gmail":
                account = self.store.get_account(public["id"])
                if account:
                    generations[account["email"].strip().lower()] = account["generation"]
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        payload = {"verifier": verifier, "redirect_uri": self.settings.redirect_uri,
                   "expected_account_id": expected_account_id,
                   "expected_email": expected.get("email") if expected else None,
                   "expected_generation": expected.get("generation") if expected else None,
                   "account_generations": generations}
        self.store.save_auth_state(_hash(state), browser_hash, self._get_vault().encrypt(payload), time.time() + _STATE_SECONDS)
        query = {"client_id": self.settings.client_id, "redirect_uri": self.settings.redirect_uri,
                 "response_type": "code", "scope": READONLY_SCOPE, "state": state,
                 "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline",
                 "prompt": "consent", "include_granted_scopes": "true"}
        if expected:
            query["login_hint"] = expected["email"]
        return {"authorization_url": AUTHORIZATION_URL + "?" + urlencode(query), "state": state}

    def _exchange(self, fields: dict) -> dict:
        return request_json(self._request, "POST", TOKEN_URL,
                            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                            body=urlencode(fields).encode("ascii"), oauth=True)

    def _client(self, access_token: str):
        if self._client_factory is GmailClient:
            return GmailClient(access_token, request=self._request)
        return self._client_factory(access_token)

    def complete(self, state: str, code: str, browser_nonce: str) -> dict:
        self._require_configuration()
        session = self.store.consume_auth_state(_hash(state), _hash(browser_nonce))
        if session is None or session.get("expires_at", 0) <= time.time():
            raise GmailAuthError("This Gmail sign-in session expired or was already used. Start sign-in again.")
        payload = self._get_vault().decrypt(session["payload"])
        if payload.get("redirect_uri") != self.settings.redirect_uri:
            raise GmailAuthError("Gmail sign-in configuration changed. Start sign-in again.")
        if not isinstance(code, str) or not code or len(code) > 8192 or any(ord(char) < 32 for char in code):
            raise GmailAuthError("Google did not return a valid sign-in code. Start sign-in again.")
        expected_id = payload.get("expected_account_id")
        if expected_id:
            expected = self.store.get_account(expected_id)
            if not expected or expected.get("generation") != payload.get("expected_generation"):
                raise GmailAuthError("The mailbox connection changed. Start sign-in again.")
        result = self._exchange({"grant_type": "authorization_code", "code": code,
                                 "client_id": self.settings.client_id, "client_secret": self.settings.client_secret,
                                 "redirect_uri": self.settings.redirect_uri, "code_verifier": payload["verifier"]})
        access_token = _token(result.get("access_token"))
        client = self._client(access_token)
        try:
            profile = client.profile()
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        email = profile.get("emailAddress", "").strip().lower()
        if not email or (expected_id and email != str(payload.get("expected_email", "")).strip().lower()):
            raise GmailAuthError("Google signed in a different mailbox. Reconnect using the selected Gmail account.")
        if expected_id:
            current = self.store.get_account(expected_id)
            if not current or current.get("generation") != payload.get("expected_generation"):
                raise GmailAuthError("The mailbox connection changed during sign-in. Start sign-in again.")
        old_refresh = None
        # Google can omit refresh_token on subsequent grants. Only the same
        # authenticated profile may reuse this mailbox's existing credential.
        for account in self.store.list_accounts():
            if account.get("provider") == "gmail" and account.get("email", "").strip().lower() == email:
                blob = self.store.get_account_tokens(account["id"])
                if blob and not result.get("refresh_token"):
                    old_refresh = self._get_vault().decrypt(blob).get("refresh_token")
                break
        tokens = _tokens(result, old_refresh=old_refresh)
        expected_generation = (payload["expected_generation"] if expected_id
                               else payload.get("account_generations", {}).get(email, ""))
        account = self.store.upsert_gmail_account(
            profile, self._get_vault().encrypt(tokens), expected_generation=expected_generation,
        )
        if account is None:
            raise GmailAuthError("The mailbox connection changed during sign-in. Start sign-in again.")
        return account

    def _account_tokens(self, account_id: str) -> tuple[dict, dict]:
        account = self.store.get_account(account_id)
        if not account or account.get("provider") != "gmail" or not account.get("connected"):
            raise GmailAuthError("This Gmail account is disconnected. Reconnect it to sync mail.")
        blob = self.store.get_account_tokens(account_id)
        if not blob:
            raise GmailAuthError("This Gmail account needs authorization. Reconnect it to sync mail.")
        tokens = self._get_vault().decrypt(blob)
        _token(tokens.get("access_token"))
        _token(tokens.get("refresh_token"))
        expiry = tokens.get("expires_at")
        if type(expiry) not in {int, float} or not math.isfinite(expiry):
            raise GmailAuthError("The Gmail token expiry is invalid. Reconnect the account.")
        return account, tokens

    def access_token(self, account_id: str) -> str:
        self._require_configuration()
        with self._locks_guard:
            lock = self._refresh_locks.setdefault(account_id, threading.Lock())
        with lock:
            account, tokens = self._account_tokens(account_id)
            if tokens["expires_at"] > time.time() + _REFRESH_MARGIN:
                return tokens["access_token"]
            result = self._exchange({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                     "client_id": self.settings.client_id, "client_secret": self.settings.client_secret})
            refreshed = _tokens(result, old_refresh=tokens["refresh_token"])
            saved = self.store.set_account_tokens(account_id, self._get_vault().encrypt(refreshed), account["generation"])
            if not saved:
                # A reconnect or disconnect during HTTP wins over this refresh.
                _account, current = self._account_tokens(account_id)
                if current["expires_at"] <= time.time() + _REFRESH_MARGIN:
                    raise GmailAuthError("The Gmail connection changed during refresh. Try syncing again.")
                return current["access_token"]
            return refreshed["access_token"]

    def disconnect(self, account_id: str) -> dict:
        account = self.store.get_account(account_id)
        if not account or account.get("provider") != "gmail":
            raise GmailAuthError("Select a connected Gmail account to disconnect.")
        return self.store.disconnect_account(account_id)
