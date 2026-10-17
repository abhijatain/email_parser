"""HTTP behavior for durable mailbox state and browser-bound Gmail sign-in.

These tests exercise the real local HTTP server and SQLite store. OAuth and
mailbox transport are deliberately fake: no Google account or network is used.
"""

from datetime import datetime, timezone
from http.cookies import SimpleCookie
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from email_parser.gmail import GmailAuthError
from email_parser.server import create_server


IMPORT = (
    b"From: Mailbox Sender <sender@example.com>\r\n"
    b"To: Owner <owner@example.com>\r\n"
    b"Subject: Durable mailbox project review\r\n"
    b"Date: Fri, 16 Oct 2026 13:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Please review this project proposal today.\r\n"
)


class FakeAuth:
    """Record the HTTP/auth boundary while enforcing one-use browser binding."""

    def __init__(self):
        self.settings = SimpleNamespace(
            configured=True,
            redirect_uri="http://127.0.0.1:8000/auth/gmail/callback",
            client_id="local-test-client-id",
            client_secret="private-client-secret-never-in-http",
        )
        self.store = None
        self.pending = {}
        self.beginnings = []
        self.completions = []
        self.disconnections = []

    def configuration(self):
        return {
            "configured": self.settings.configured,
            "redirect_uri": self.settings.redirect_uri,
            "missing": [] if self.settings.configured else ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
        }

    def begin(self, browser_nonce, expected_account_id=None):
        state = "fake-state-" + str(len(self.beginnings) + 1)
        self.beginnings.append((browser_nonce, expected_account_id))
        self.pending[state] = (browser_nonce, expected_account_id)
        return {
            "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({"state": state}),
            "state": state,
        }

    def complete(self, state, code, browser_nonce):
        self.completions.append((state, code, browser_nonce))
        expected = self.pending.get(state)
        if expected is None or not browser_nonce or expected[0] != browser_nonce:
            raise GmailAuthError("Gmail sign-in could not be verified. Please try again.")
        del self.pending[state]
        if code != "valid-test-code":
            raise GmailAuthError("Gmail sign-in could not be completed. Please try again.")
        if expected[1] is not None:
            return self.store.get_account(expected[1])
        return self.store.upsert_gmail_account(
            {"emailAddress": "signed-in@example.com", "historyId": "100"},
            "encrypted-for-http-tests",
            now=datetime(2026, 10, 17, tzinfo=timezone.utc),
        )

    def disconnect(self, account_id):
        self.disconnections.append(account_id)
        return self.store.disconnect_account(account_id)


class FakeMailbox:
    def __init__(self):
        self.queued = []
        self.closed = False

    def enqueue(self, account_id):
        self.queued.append(account_id)
        return self.status(account_id)

    def status(self, account_id=None):
        return {
            "state": "queued" if account_id in self.queued else "idle",
            "account_id": account_id,
            "processed": 0,
            "added": 0,
            "error": None,
        }

    def close(self):
        self.closed = True


class AccountServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "inbox.sqlite3"
        self.server = None
        self.thread = None
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.stop_server)
        self.start_server()

    def start_server(self, *, redirect_uri=None):
        self.auth = FakeAuth()
        self.mailbox = FakeMailbox()
        if redirect_uri is not None:
            self.auth.settings.redirect_uri = redirect_uri
        self.server = create_server("127.0.0.1", 0, self.db_path, auth=self.auth, mailbox=self.mailbox)
        self.auth.store = self.server.store
        self.host, self.port = self.server.server_address[:2]
        self.origin = f"http://{self.host}:{self.port}"
        if redirect_uri is None:
            self.auth.settings.redirect_uri = self.origin + "/auth/gmail/callback"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.server = None

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_request(self, method, path, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload).encode()
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        status, response_headers, raw = self.request(method, path, body, request_headers)
        self.assertIn("application/json", response_headers.get("Content-Type", ""))
        return status, response_headers, json.loads(raw)

    def new_account(self, email="one@example.com"):
        return self.server.store.upsert_gmail_account(
            {"emailAddress": email, "historyId": "100"},
            "opaque-encrypted-tokens-" + email,
            now=datetime(2026, 10, 17, tzinfo=timezone.utc),
        )

    def import_to(self, account_id):
        status, _, raw = self.request(
            "POST", "/api/import?" + urlencode({"account": account_id}), IMPORT,
            {"Content-Type": "message/rfc822"},
        )
        self.assertEqual(status, 200, raw)
        return json.loads(raw)["message"]

    def inbox(self, account_id, **filters):
        status, _, body = self.json_request("GET", "/api/inbox?" + urlencode({"account": account_id, **filters}))
        self.assertEqual(status, 200, body)
        return body

    def begin(self, payload=None):
        status, headers, body = self.json_request(
            "POST", "/api/accounts/gmail/start", payload or {}, {"Origin": self.origin},
        )
        self.assertEqual(status, 200, body)
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        nonce = cookie["inbox_oauth"].value
        state = parse_qs(urlsplit(body["authorization_url"]).query)["state"][0]
        return body, headers, cookie["inbox_oauth"], nonce, state

    def callback(self, state, nonce=None, code="valid-test-code"):
        headers = {"Cookie": "inbox_oauth=" + nonce} if nonce is not None else {}
        return self.request("GET", "/auth/gmail/callback?" + urlencode({"state": state, "code": code}), headers=headers)

    def test_public_account_lists_and_inbox_keep_private_state_on_server(self):
        account = self.new_account()
        self.server.store.set_active_account_id(account["id"])
        status, _, body = self.json_request("GET", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(body["active_account_id"], account["id"])
        self.assertEqual({row["id"] for row in body["accounts"]}, {"demo", account["id"]})
        self.assertTrue(body["configuration"]["configured"])
        inbox = self.inbox(account["id"])
        self.assertEqual(inbox["account"]["email"], "one@example.com")
        self.assertEqual(inbox["messages"], [])
        self.assertEqual(inbox["summary"]["total"], 0)
        self.assertEqual(inbox["mailbox"]["account_id"], account["id"])
        for public in [body, inbox]:
            encoded = json.dumps(public)
            self.assertNotIn("opaque-encrypted-tokens", encoded)
            self.assertNotIn(self.auth.settings.client_secret, encoded)
            for private_key in ["generation", "history_id", "initial_since", "initial_before", "token_blob"]:
                self.assertNotIn('"' + private_key + '"', encoded)

    def test_import_deduplication_and_read_write_ownership_are_account_scoped(self):
        first = self.new_account()
        second = self.new_account("two@example.com")
        first_message = self.import_to(first["id"])
        self.assertEqual(self.import_to(first["id"])["id"], first_message["id"])
        second_message = self.import_to(second["id"])
        self.assertNotEqual(first_message["id"], second_message["id"])
        for account, message in [(first, first_message), (second, second_message)]:
            inbox = self.inbox(account["id"])
            self.assertEqual([row["id"] for row in inbox["messages"]], [message["id"]])
            self.assertEqual(inbox["summary"]["total"], 1)
        path = "/api/messages/" + first_message["id"]
        for method, payload in [("GET", None), ("PATCH", {"unread": False})]:
            status, _, body = self.json_request(method, path + "?" + urlencode({"account": second["id"]}), payload)
            self.assertEqual(status, 404)
            self.assertTrue(body["error"])
        self.assertTrue(self.inbox(first["id"])["messages"][0]["unread"])
        self.assertEqual(self.inbox("demo")["summary"]["total"], 28)

    def test_message_decisions_selection_and_per_account_preferences_survive_restart(self):
        account = self.new_account()
        message = self.import_to(account["id"])
        scoped = "?" + urlencode({"account": account["id"]})
        decisions = {"unread": False, "starred": True, "archived": True, "category": "personal", "priority": "low"}
        status, _, saved = self.json_request("PATCH", "/api/messages/" + message["id"] + scoped, decisions)
        self.assertEqual(status, 200)
        for field, value in decisions.items():
            self.assertEqual(saved[field], value)
        preferences = {"view": "archived", "mode": "all", "category": "personal", "priority": "low", "search": "Durable"}
        status, _, _ = self.json_request("PATCH", "/api/preferences" + scoped, preferences)
        self.assertEqual(status, 200)
        status, _, _ = self.json_request("POST", "/api/accounts/select", {"account_id": account["id"]})
        self.assertEqual(status, 200)
        old_mailbox = self.mailbox
        self.stop_server()
        self.assertTrue(old_mailbox.closed)
        self.start_server()
        status, _, accounts = self.json_request("GET", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(accounts["active_account_id"], account["id"])
        status, _, reloaded = self.json_request("GET", "/api/messages/" + message["id"] + scoped)
        self.assertEqual(status, 200)
        for field, value in decisions.items():
            self.assertEqual(reloaded[field], value)
        status, _, reloaded_preferences = self.json_request("GET", "/api/preferences" + scoped)
        self.assertEqual(status, 200)
        self.assertEqual(reloaded_preferences, preferences)
        self.assertNotEqual(self.json_request("GET", "/api/preferences?account=demo")[2], preferences)

    def test_preferences_and_account_queries_validate_without_changing_saved_values(self):
        account = self.new_account()
        scoped = "?" + urlencode({"account": account["id"]})
        before = self.json_request("GET", "/api/preferences" + scoped)[2]
        for invalid in [{"mode": "random"}, {"view": "trash"}, {"category": "invalid"}, {"priority": "urgent"}, {"search": "a" * 501}, {"search": False}, {"secret": "no"}]:
            with self.subTest(invalid=invalid):
                status, _, body = self.json_request("PATCH", "/api/preferences" + scoped, invalid)
                self.assertEqual(status, 400)
                self.assertTrue(body["error"])
        self.assertEqual(self.json_request("GET", "/api/preferences" + scoped)[2], before)
        for path in ["/api/inbox?account=unknown", "/api/preferences?account=unknown"]:
            self.assertEqual(self.json_request("GET", path)[0], 404)
        for path in ["/api/inbox?account=demo&account=demo", "/api/preferences?account=demo&account=demo"]:
            self.assertEqual(self.json_request("GET", path)[0], 400)
        self.assertEqual(self.json_request("POST", "/api/accounts/select", {"account_id": "unknown"})[0], 404)

    def test_new_account_mutations_require_same_origin(self):
        account = self.new_account()
        targets = [
            ("POST", "/api/accounts/select", {"account_id": account["id"]}),
            ("PATCH", "/api/preferences?" + urlencode({"account": account["id"]}), {"view": "unread"}),
            ("POST", "/api/accounts/" + account["id"] + "/sync", {}),
            ("POST", "/api/accounts/" + account["id"] + "/disconnect", {}),
            ("POST", "/api/accounts/gmail/start", {}),
        ]
        for method, path, payload in targets:
            with self.subTest(path=path):
                status, _, body = self.json_request(method, path, payload, {"Origin": "https://attacker.example"})
                self.assertEqual(status, 403)
                self.assertTrue(body["error"])
        self.assertEqual(self.mailbox.queued, [])
        self.assertEqual(self.auth.beginnings, [])
        self.assertEqual(self.auth.disconnections, [])
        self.assertTrue(self.server.store.get_account(account["id"])["connected"])

    def test_rebinding_host_cannot_read_mail_or_mutate_with_its_matching_origin(self):
        message = self.inbox("demo")["messages"][0]
        before = self.server.store.get_message(message["id"])
        hostile_host = "attacker.example:" + str(self.port)
        for browser_headers in [{"Host": hostile_host}, {"Host": hostile_host, "Origin": "http://" + hostile_host}]:
            with self.subTest(headers=browser_headers):
                status, _, body = self.json_request("GET", "/api/inbox?account=demo", headers=browser_headers)
                self.assertEqual(status, 403)
                self.assertTrue(body["error"])
                self.assertNotIn(message["subject"], json.dumps(body))
        status, _, body = self.json_request(
            "PATCH", "/api/messages/" + message["id"] + "?account=demo", {"unread": not before["unread"]},
            {"Host": hostile_host, "Origin": "http://" + hostile_host},
        )
        self.assertEqual(status, 403)
        self.assertTrue(body["error"])
        self.assertEqual(self.server.store.get_message(message["id"]), before)

    def test_loopback_hostnames_remain_usable_for_reads_and_same_origin_changes(self):
        message = self.inbox("demo")["messages"][0]
        for hostname in ["127.0.0.1", "localhost", "LOCALHOST"]:
            host = hostname + ":" + str(self.port)
            with self.subTest(host=host):
                status, _, inbox = self.json_request("GET", "/api/inbox?account=demo", headers={"Host": host})
                self.assertEqual(status, 200)
                self.assertEqual(inbox["account"]["id"], "demo")
                status, _, changed = self.json_request(
                    "PATCH", "/api/messages/" + message["id"] + "?account=demo", {"starred": True},
                    {"Host": host, "Origin": "http://" + host},
                )
                self.assertEqual(status, 200)
                self.assertTrue(changed["starred"])

    def test_configured_https_proxy_host_is_trusted_for_reads_changes_and_gmail_start(self):
        proxy_host = "inbox.tests.example"
        proxy_origin = "https://" + proxy_host
        self.stop_server()
        self.start_server(redirect_uri=proxy_origin + "/auth/gmail/callback")
        headers = {"Host": proxy_host, "Origin": proxy_origin}
        status, _, inbox = self.json_request("GET", "/api/inbox?account=demo", headers=headers)
        self.assertEqual(status, 200)
        message = inbox["messages"][0]
        status, _, changed = self.json_request(
            "PATCH", "/api/messages/" + message["id"] + "?account=demo", {"starred": True}, headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(changed["starred"])
        status, response_headers, body = self.json_request("POST", "/api/accounts/gmail/start", {}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(urlsplit(body["authorization_url"]).netloc, "accounts.google.com")
        cookie = SimpleCookie()
        cookie.load(response_headers["Set-Cookie"])
        self.assertTrue(cookie["inbox_oauth"]["secure"])
        self.assertEqual(len(self.auth.beginnings), 1)

    def test_sync_and_disconnect_retain_cached_mail_and_local_choices(self):
        account = self.new_account()
        message = self.import_to(account["id"])
        scoped = "?" + urlencode({"account": account["id"]})
        status, _, _ = self.json_request("PATCH", "/api/messages/" + message["id"] + scoped, {"starred": True, "unread": False})
        self.assertEqual(status, 200)
        status, _, sync = self.json_request("POST", "/api/accounts/" + account["id"] + "/sync", {})
        self.assertEqual(status, 200)
        self.assertEqual(sync["sync"]["account_id"], account["id"])
        self.assertEqual(self.mailbox.queued, [account["id"]])
        status, _, disconnected = self.json_request("POST", "/api/accounts/" + account["id"] + "/disconnect", {})
        self.assertEqual(status, 200)
        self.assertFalse(disconnected["account"]["connected"])
        self.assertIsNone(self.server.store.get_account_tokens(account["id"]))
        cached = self.inbox(account["id"])["messages"]
        self.assertEqual([row["id"] for row in cached], [message["id"]])
        self.assertTrue(cached[0]["starred"])
        self.assertFalse(cached[0]["unread"])

    def test_oauth_start_sets_restricted_cookie_and_returns_only_browser_authorization_url(self):
        body, _, cookie, nonce, _ = self.begin()
        self.assertEqual(set(body), {"authorization_url"})
        self.assertEqual(urlsplit(body["authorization_url"]).netloc, "accounts.google.com")
        self.assertGreaterEqual(len(nonce), 32)
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"].lower(), "lax")
        self.assertEqual(cookie["path"], "/auth/gmail/callback")
        self.assertFalse(cookie["secure"])
        self.assertEqual(self.auth.beginnings, [(nonce, None)])
        self.assertNotIn(nonce, json.dumps(body))
        self.assertNotIn(self.auth.settings.client_secret, json.dumps(body))

    def test_oauth_callback_selects_account_queues_import_and_clears_browser_cookie(self):
        _, _, _, nonce, state = self.begin()
        status, headers, body = self.callback(state, nonce)
        self.assertEqual(status, 303)
        redirect = urlsplit(headers["Location"])
        self.assertFalse(redirect.netloc)
        self.assertEqual(redirect.path, "/")
        result = parse_qs(redirect.query)
        self.assertEqual(result["gmail"], ["connected"])
        account_id = result["account"][0]
        self.assertEqual(self.server.store.get_active_account_id(), account_id)
        self.assertEqual(self.mailbox.queued, [account_id])
        self.assertEqual(self.auth.completions, [(state, "valid-test-code", nonce)])
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        self.assertEqual(cookie["inbox_oauth"].value, "")
        self.assertEqual(cookie["inbox_oauth"]["max-age"], "0")
        self.assertNotIn(b"valid-test-code", body)
        self.assertNotIn("valid-test-code", headers["Location"])
        replay_status, replay_headers, replay_body = self.callback(state, nonce)
        self.assertIn(replay_status, {303, 400})
        self.assertNotIn("gmail=connected", replay_headers.get("Location", ""))
        self.assertNotIn(b"valid-test-code", replay_body)
        self.assertEqual(self.mailbox.queued, [account_id])

    def test_callback_cannot_use_state_from_another_browser(self):
        _, _, _, nonce, state = self.begin()
        for wrong_nonce in [None, "another-browser-nonce"]:
            with self.subTest(wrong_nonce=wrong_nonce):
                status, headers, body = self.callback(state, wrong_nonce)
                self.assertIn(status, {303, 400})
                self.assertNotIn("gmail=connected", headers.get("Location", ""))
                self.assertNotIn(b"valid-test-code", body)
                self.assertEqual(self.mailbox.queued, [])
                self.assertEqual(self.server.store.get_active_account_id(), "demo")
        self.assertEqual(self.callback(state, nonce)[0], 303)
        self.assertEqual(len(self.mailbox.queued), 1)

    def test_denied_and_malformed_callbacks_do_not_echo_provider_details(self):
        private_text = "private-authorization-code-or-provider-description"
        paths = [
            "/auth/gmail/callback?" + urlencode({"error": "access_denied", "error_description": private_text}),
            "/auth/gmail/callback?" + urlencode({"state": "unknown", "code": private_text}),
            "/auth/gmail/callback?" + urlencode({"state": "one", "code": private_text}) + "&state=two",
            "/auth/gmail/callback",
        ]
        for path in paths:
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertIn(status, {303, 400})
                self.assertNotIn(private_text.encode(), body)
                self.assertNotIn(private_text, headers.get("Location", ""))
                self.assertFalse(urlsplit(headers.get("Location", "")).netloc)
        self.assertEqual(self.mailbox.queued, [])

    def test_unconfigured_oauth_reports_environment_names_and_never_starts_consent(self):
        self.auth.settings.configured = False
        status, _, accounts = self.json_request("GET", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertFalse(accounts["configuration"]["configured"])
        self.assertIn("GOOGLE_CLIENT_SECRET", accounts["configuration"]["missing"])
        status, headers, body = self.json_request("POST", "/api/accounts/gmail/start", {}, {"Origin": self.origin})
        self.assertEqual(status, 409)
        self.assertTrue(body["error"])
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.auth.beginnings, [])
        self.assertNotIn(self.auth.settings.client_secret, json.dumps(accounts))
        self.assertNotIn(self.auth.settings.client_secret, json.dumps(body))

    def test_start_requires_configured_callback_origin_before_setting_cookie(self):
        self.auth.settings.redirect_uri = "http://localhost:8000/auth/gmail/callback"
        status, headers, body = self.json_request("POST", "/api/accounts/gmail/start", {}, {"Origin": self.origin})
        self.assertEqual(status, 409)
        self.assertTrue(body["error"])
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.auth.beginnings, [])

    def test_https_callback_origin_uses_secure_browser_cookie(self):
        secure_origin = self.origin.replace("http://", "https://", 1)
        self.auth.settings.redirect_uri = secure_origin + "/auth/gmail/callback"
        status, headers, body = self.json_request(
            "POST", "/api/accounts/gmail/start", {}, {"Origin": secure_origin},
        )
        self.assertEqual(status, 200, body)
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        self.assertTrue(cookie["inbox_oauth"]["secure"])
        self.assertTrue(cookie["inbox_oauth"]["httponly"])

    def test_get_cannot_start_browser_consent(self):
        status, _, _ = self.request("GET", "/api/accounts/gmail/start")
        self.assertIn(status, {404, 405})
        self.assertEqual(self.auth.beginnings, [])

    def test_head_callback_does_not_consume_pending_sign_in(self):
        _, _, _, nonce, state = self.begin()
        path = "/auth/gmail/callback?" + urlencode({"state": state, "code": "valid-test-code"})
        status, _, body = self.request("HEAD", path, headers={"Cookie": "inbox_oauth=" + nonce})
        self.assertEqual(status, 405)
        self.assertEqual(body, b"")
        self.assertEqual(self.auth.completions, [])
        self.assertEqual(self.mailbox.queued, [])
        self.assertEqual(self.callback(state, nonce)[0], 303)
        self.assertEqual(len(self.mailbox.queued), 1)

    def test_reconnect_passes_expected_identity_to_authentication_service(self):
        account = self.new_account()
        _, _, _, _, _ = self.begin({"account_id": account["id"]})
        self.assertEqual(self.auth.beginnings[0][1], account["id"])

    def test_factory_defaults_keep_integrations_off_even_with_credentials_and_saved_accounts(self):
        account = self.new_account()
        self.stop_server()
        with patch.dict(os.environ, {
            "GOOGLE_CLIENT_ID": "fake-present-client-id",
            "GOOGLE_CLIENT_SECRET": "fake-present-client-secret",
            "TYPESAFE_API_KEY": "fake-present-typesafe-key",
        }), patch("email_parser.server.from_environment", side_effect=AssertionError("AI auto-discovery")), patch(
            "email_parser.server.GmailSettings.from_environment", side_effect=AssertionError("Gmail auto-discovery"),
        ), patch("email_parser.gmail_auth.GmailAuthService.access_token") as access_token:
            offline_server = create_server("127.0.0.1", 0, self.db_path)
            try:
                self.assertFalse(offline_server.auth.configuration()["configured"])
                self.assertFalse(offline_server.classification.status()["configured"])
                self.assertFalse(offline_server.mailbox.enabled)
                offline_server.mailbox.enqueue(account["id"])
                access_token.assert_not_called()
                self.assertIsNotNone(offline_server.store.get_account(account["id"]))
            finally:
                offline_server.server_close()

    def test_failed_bind_starts_no_integrations_and_closes_supplied_classifier(self):
        classifier = Mock()
        supplied_mailbox = FakeMailbox()
        with patch("email_parser.server.ClassificationService") as classification, patch(
            "email_parser.server.MailboxService",
        ) as mailbox, patch("email_parser.server.GmailSettings.from_environment") as settings:
            with self.assertRaises(OSError):
                create_server(
                    self.host, self.port, self.db_path,
                    classifier=classifier, mailbox=supplied_mailbox, enable_mail=True,
                )
        classifier.close.assert_called_once_with()
        self.assertTrue(supplied_mailbox.closed)
        classification.assert_not_called()
        mailbox.assert_not_called()
        settings.assert_not_called()

    def test_failed_mailbox_start_closes_classification_and_releases_bound_socket(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            available_port = reservation.getsockname()[1]
        classification = Mock()
        with patch("email_parser.server.ClassificationService", return_value=classification), patch(
            "email_parser.server.MailboxService", side_effect=GmailAuthError("Cannot start test mailbox."),
        ):
            with self.assertRaises(GmailAuthError):
                create_server("127.0.0.1", available_port, self.db_path)
        classification.close.assert_called_once_with()
        # A second server can immediately bind the failed server's address.
        replacement = create_server("127.0.0.1", available_port, self.db_path)
        replacement.server_close()


if __name__ == "__main__":
    unittest.main()
