"""Browser-bound Gmail OAuth and encrypted durable credentials; fake HTTP only."""

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from email_parser.gmail import GmailAuthError, GmailError, READONLY_SCOPE
from email_parser.gmail_auth import GmailAuthService, GmailSettings, TokenVault, TOKEN_URL
from email_parser.store import InboxStore


class OAuthTransport:
    def __init__(self):
        self.calls = []
        self.profile = {"emailAddress": "alex@example.com", "historyId": "123"}
        self.token_result = {"access_token": "access-secret", "refresh_token": "refresh-secret",
                             "expires_in": 3600, "token_type": "Bearer", "scope": READONLY_SCOPE}
        self.status = 200
        self.on_exchange = None

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url == TOKEN_URL:
            if self.on_exchange:
                self.on_exchange()
            return self.status, {}, json.dumps(self.token_result).encode()
        if url.endswith("/profile"):
            return 200, {}, json.dumps(self.profile).encode()
        raise AssertionError("Unexpected endpoint")


class GmailAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.store = InboxStore(self.path / "inbox.sqlite3")
        self.settings = GmailSettings("client-id", "client-secret")
        self.transport = OAuthTransport()
        self.auth = GmailAuthService(self.store, self.settings, request=self.transport)

    def connect(self, expected=None, nonce="browser-random-nonce"):
        start = self.auth.begin(nonce, expected)
        return self.auth.complete(start["state"], "one-use-code", nonce)

    def test_unconfigured_is_usable_and_never_creates_key(self):
        auth = GmailAuthService(self.store, GmailSettings())
        self.assertEqual(auth.configuration(), {"configured": False, "redirect_uri": self.settings.redirect_uri,
                                               "missing": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]})
        self.assertFalse((self.path / ".inbox-token-key").exists())
        with self.assertRaises(GmailAuthError):
            auth.begin("browser")

    def test_settings_environment_aliases_and_safe_repr(self):
        with patch.dict(os.environ, {"INBOX_GOOGLE_CLIENT_ID": "alias-id", "GMAIL_CLIENT_SECRET": "alias-secret"}, clear=True):
            settings = GmailSettings.from_environment(self.settings.redirect_uri)
        self.assertEqual(settings.client_id, "alias-id")
        self.assertEqual(settings.client_secret, "alias-secret")
        self.assertTrue(settings.configured)
        self.assertNotIn("alias-secret", repr(settings))
        self.assertNotIn("client-secret", repr(self.settings))
        self.assertNotIn("client-secret", json.dumps(self.auth.configuration()))

    def test_redirect_uri_restrictions(self):
        for uri in ("http://127.0.0.1:8000/auth/gmail/callback", "http://localhost:3000/callback", "https://inbox.example/callback"):
            self.assertEqual(GmailSettings(redirect_uri=uri).redirect_uri, uri)
        for uri in ("http://inbox.example/callback", "https://u:p@inbox.example/callback", "https://example/cb?x=y",
                    "https://example/cb#fragment", "javascript:evil", "https://example:bad/cb", "https://example/cb?", "https://example/cb\n"):
            with self.subTest(uri=uri), self.assertRaises(GmailAuthError):
                GmailSettings(redirect_uri=uri)

    def test_vault_key_is_private_stable_and_ciphertext_authenticated(self):
        key_path = self.path / "key"
        vault = TokenVault(key_path)
        key = key_path.read_bytes()
        blob = vault.encrypt({"refresh_token": "refresh-secret"})
        self.assertNotIn("refresh-secret", blob)
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(TokenVault(key_path).decrypt(blob), {"refresh_token": "refresh-secret"})
        self.assertEqual(key_path.read_bytes(), key)
        with self.assertRaises(GmailAuthError):
            vault.decrypt(blob[:-1] + ("A" if blob[-1] != "A" else "B"))
        key_path.write_bytes(b"corrupted")
        with self.assertRaises(GmailAuthError):
            TokenVault(key_path)
        self.assertEqual(key_path.read_bytes(), b"corrupted")

    def test_vault_rejects_symlink_without_modifying_target(self):
        target = self.path / "target"
        target.write_bytes(b"unchanged")
        link = self.path / "key-link"
        link.symlink_to(target)
        with self.assertRaises(GmailAuthError):
            TokenVault(link)
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_begin_requests_readonly_offline_pkce_and_encrypts_verifier(self):
        start = self.auth.begin("browser-random")
        uri = urlsplit(start["authorization_url"])
        query = parse_qs(uri.query)
        self.assertEqual((uri.scheme, uri.hostname), ("https", "accounts.google.com"))
        self.assertEqual(query["scope"], [READONLY_SCOPE])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], [start["state"]])
        self.assertNotIn("client_secret", query)
        self.assertNotIn("code_verifier", query)
        self.auth.complete(start["state"], "code", "browser-random")
        exchange = parse_qs(self.transport.calls[0][2]["body"].decode())
        verifier = exchange["code_verifier"][0]
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(query["code_challenge"], [expected])
        self.assertEqual(exchange["grant_type"], ["authorization_code"])
        self.assertEqual(exchange["client_secret"], ["client-secret"])
        self.assertNotIn(verifier.encode(), self.store.db_path.read_bytes())

    def test_wrong_browser_does_not_consume_state_and_replay_fails(self):
        start = self.auth.begin("browser-one")
        with self.assertRaises(GmailAuthError):
            self.auth.complete(start["state"], "code", "browser-two")
        self.assertEqual(self.transport.calls, [])
        self.auth.complete(start["state"], "code", "browser-one")
        with self.assertRaises(GmailAuthError):
            self.auth.complete(start["state"], "code", "browser-one")
        self.assertEqual(len(self.transport.calls), 2)

    def test_state_expiry_and_unknown_state_fail_before_http(self):
        start = self.auth.begin("browser")
        with patch("email_parser.gmail_auth.time.time", return_value=time.time() + 601), self.assertRaises(GmailAuthError):
            self.auth.complete(start["state"], "code", "browser")
        with self.assertRaises(GmailAuthError):
            self.auth.complete("unknown-state", "code", "browser")
        self.assertEqual(self.transport.calls, [])

    def test_completed_account_uses_authenticated_profile_and_encrypted_credentials(self):
        account = self.connect()
        self.assertEqual(account["email"], "alex@example.com")
        self.assertTrue(account["connected"])
        self.assertNotIn("token_blob", account)
        blob = self.store.get_account_tokens(account["id"])
        self.assertEqual(self.auth._get_vault().decrypt(blob)["refresh_token"], "refresh-secret")
        for secret in ("refresh-secret", "access-secret", "client-secret", "one-use-code"):
            self.assertNotIn(secret.encode(), self.store.db_path.read_bytes())
        self.assertEqual(self.auth.access_token(account["id"]), "access-secret")
        self.assertEqual(len(self.transport.calls), 2)

    def test_auth_and_pending_state_survive_service_restart(self):
        start = self.auth.begin("browser")
        restarted = GmailAuthService(InboxStore(self.store.db_path), self.settings, request=self.transport)
        account = restarted.complete(start["state"], "code", "browser")
        restarted_again = GmailAuthService(InboxStore(self.store.db_path), self.settings, request=self.transport)
        self.assertEqual(restarted_again.access_token(account["id"]), "access-secret")

    def test_new_account_requires_persistent_refresh_token_and_readonly_scope(self):
        for change in ({"refresh_token": None}, {"scope": "openid email"}, {"expires_in": 0},
                       {"expires_in": float("nan")}, {"token_type": None}):
            self.transport.token_result = {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": READONLY_SCOPE, **change}
            with self.subTest(change=change), self.assertRaises(GmailAuthError):
                self.connect()
        self.assertEqual(len(self.store.list_accounts()), 1)

    def test_expected_reconnect_rejects_a_different_google_identity(self):
        account = self.connect()
        start = self.auth.begin("browser", account["id"])
        self.transport.profile["emailAddress"] = "other@example.com"
        with self.assertRaises(GmailAuthError):
            self.auth.complete(start["state"], "code", "browser")
        self.assertEqual(len(self.store.list_accounts()), 2)
        self.assertEqual(self.store.get_account(account["id"])["generation"], account["generation"])

    def test_reconnect_reuses_same_mailbox_refresh_token_and_keeps_history(self):
        account = self.connect()
        self.store.update_account_sync(account["id"], {"history_id": "567", "initial_import_complete": True}, account["generation"])
        self.transport.token_result.pop("refresh_token")
        reconnected = self.connect(account["id"])
        self.assertEqual(reconnected["id"], account["id"])
        self.assertEqual(reconnected["history_id"], "567")
        self.assertTrue(reconnected["initial_import_complete"])
        self.assertNotEqual(reconnected["generation"], account["generation"])
        self.assertEqual(self.auth._get_vault().decrypt(self.store.get_account_tokens(account["id"]))["refresh_token"], "refresh-secret")

    def test_disconnect_during_reconnect_exchange_is_not_resurrected(self):
        account = self.connect()
        start = self.auth.begin("browser", account["id"])
        self.transport.on_exchange = lambda: self.store.disconnect_account(account["id"])
        with self.assertRaises(GmailAuthError):
            self.auth.complete(start["state"], "code", "browser")
        self.assertFalse(self.store.get_account(account["id"])["connected"])
        self.assertIsNone(self.store.get_account_tokens(account["id"]))

    def test_generic_connect_rejects_disconnect_while_exchange_is_blocked(self):
        account = self.connect()
        start = self.auth.begin("browser")
        reached_http, release_http = threading.Event(), threading.Event()
        errors = []

        def blocked_exchange():
            reached_http.set()
            if not release_http.wait(2):
                raise RuntimeError("Fake exchange timed out")

        def complete():
            try:
                self.auth.complete(start["state"], "code", "browser")
            except Exception as error:
                errors.append(error)

        self.transport.on_exchange = blocked_exchange
        thread = threading.Thread(target=complete, daemon=True)
        thread.start()
        try:
            self.assertTrue(reached_http.wait(2))
            self.store.disconnect_account(account["id"])
        finally:
            release_http.set()
            thread.join(2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], GmailAuthError)
        self.assertFalse(self.store.get_account(account["id"])["connected"])
        self.assertIsNone(self.store.get_account_tokens(account["id"]))

    def test_new_connect_rejects_account_created_during_exchange(self):
        start = self.auth.begin("browser")
        captured = {}

        def another_connection():
            captured.update(self.store.upsert_gmail_account(self.transport.profile, "other-encrypted-blob"))

        self.transport.on_exchange = another_connection
        with self.assertRaises(GmailAuthError):
            self.auth.complete(start["state"], "code", "browser")
        self.assertEqual(self.store.get_account_tokens(captured["id"]), "other-encrypted-blob")
        self.assertEqual(self.store.get_account(captured["id"])["generation"], captured["generation"])

    def test_missing_key_never_silently_replaces_saved_connection(self):
        account = self.connect()
        key_path = self.path / ".inbox-token-key"
        key_path.unlink()
        restarted = GmailAuthService(self.store, self.settings, request=self.transport)
        with self.assertRaises(GmailAuthError):
            restarted.access_token(account["id"])
        self.assertFalse(key_path.exists())
        self.assertIsNotNone(self.store.get_account_tokens(account["id"]))

    def expire(self, account):
        tokens = self.auth._get_vault().decrypt(self.store.get_account_tokens(account["id"]))
        tokens["expires_at"] = time.time() + 30
        self.store.set_account_tokens(account["id"], self.auth._get_vault().encrypt(tokens), account["generation"])

    def test_expiring_token_refreshes_and_persists_rotated_refresh_token(self):
        account = self.connect()
        self.expire(account)
        self.transport.token_result.update(access_token="refreshed-access", refresh_token="rotated-refresh")
        self.assertEqual(self.auth.access_token(account["id"]), "refreshed-access")
        exchange = parse_qs(self.transport.calls[-1][2]["body"].decode())
        self.assertEqual(exchange["grant_type"], ["refresh_token"])
        self.assertEqual(exchange["refresh_token"], ["refresh-secret"])
        self.assertNotIn("code_verifier", exchange)
        self.assertEqual(self.auth._get_vault().decrypt(self.store.get_account_tokens(account["id"]))["refresh_token"], "rotated-refresh")

    def test_refresh_omitted_refresh_token_reuses_existing(self):
        account = self.connect()
        self.expire(account)
        self.transport.token_result.pop("refresh_token")
        self.assertEqual(self.auth.access_token(account["id"]), "access-secret")
        self.assertEqual(self.auth._get_vault().decrypt(self.store.get_account_tokens(account["id"]))["refresh_token"], "refresh-secret")

    def test_disconnect_during_refresh_rejects_stale_write(self):
        account = self.connect()
        self.expire(account)
        self.transport.on_exchange = lambda: self.store.disconnect_account(account["id"])
        with self.assertRaises(GmailAuthError):
            self.auth.access_token(account["id"])
        self.assertIsNone(self.store.get_account_tokens(account["id"]))
        self.assertFalse(self.store.get_account(account["id"])["connected"])

    def test_reconnect_during_refresh_keeps_new_generation_credentials(self):
        account = self.connect()
        self.expire(account)

        def reconnect():
            fresh = {"access_token": "newer-access", "refresh_token": "newer-refresh", "expires_at": time.time() + 3600}
            self.store.upsert_gmail_account(self.transport.profile, self.auth._get_vault().encrypt(fresh))

        self.transport.on_exchange = reconnect
        self.assertEqual(self.auth.access_token(account["id"]), "newer-access")
        self.assertEqual(self.auth._get_vault().decrypt(self.store.get_account_tokens(account["id"]))["refresh_token"], "newer-refresh")

    def test_refresh_does_not_hold_database_lock_during_http(self):
        account = self.connect()
        self.expire(account)
        written = threading.Event()

        def during_http():
            def write():
                self.store.set_active_account_id(account["id"])
                written.set()
            thread = threading.Thread(target=write, daemon=True)
            thread.start()
            self.assertTrue(written.wait(1), "HTTP request blocked a concurrent database operation")
            thread.join(1)

        self.transport.on_exchange = during_http
        self.assertEqual(self.auth.access_token(account["id"]), "access-secret")
        self.assertTrue(written.is_set())

    def test_concurrent_access_uses_one_refresh(self):
        account = self.connect()
        self.expire(account)
        barrier = threading.Barrier(5)
        values = []
        errors = []

        def access():
            try:
                barrier.wait(2)
                values.append(self.auth.access_token(account["id"]))
            except Exception as error:
                errors.append(type(error).__name__)

        threads = [threading.Thread(target=access) for _ in range(4)]
        for thread in threads:
            thread.start()
        barrier.wait(2)
        for thread in threads:
            thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(values, ["access-secret"] * 4)
        exchanges = [call for call in self.transport.calls if call[1] == TOKEN_URL]
        self.assertEqual(len(exchanges), 2)  # Initial authorization plus one refresh.

    def test_revoked_refresh_is_safe_and_does_not_replace_credentials(self):
        account = self.connect()
        self.expire(account)
        before = self.store.get_account_tokens(account["id"])
        self.transport.status = 400
        self.transport.token_result = {"error": "invalid_grant", "error_description": "PRIVATE_REFRESH_SECRET"}
        with self.assertRaises(GmailAuthError) as caught:
            self.auth.access_token(account["id"])
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(self.store.get_account_tokens(account["id"]), before)

    def test_disconnect_invalidates_tokens_but_keeps_account_and_cached_mail(self):
        account = self.connect()
        public = self.auth.disconnect(account["id"])
        self.assertFalse(public["connected"])
        self.assertIsNotNone(self.store.get_account(account["id"]))
        self.assertIsNone(self.store.get_account_tokens(account["id"]))
        with self.assertRaises(GmailAuthError):
            self.auth.access_token(account["id"])


if __name__ == "__main__":
    unittest.main()
