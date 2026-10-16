"""HTTP integration for the local dashboard and persisted inbox actions."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from email_parser.server import MAX_EMAIL_BYTES, create_server


IMPORT = (
    b"From: HTTP Sender <http.sender@example.com>\r\n"
    b"To: Alex Morgan <alex.morgan@example.com>\r\n"
    b"Subject: HTTP violet project approval\r\n"
    b"Date: Fri, 16 Oct 2026 13:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Please review the violet project proposal today.\r\n"
)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, Path(self.temporary.name) / "inbox.sqlite3")
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

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
        status, response_headers, data = self.request(method, path, body, request_headers)
        self.assertIn("application/json", response_headers.get("Content-Type", ""))
        return status, response_headers, json.loads(data)

    def inbox(self, **filters):
        query = "?" + urlencode(filters) if filters else ""
        status, _, data = self.json_request("GET", "/api/inbox" + query)
        self.assertEqual(status, 200)
        return data

    def test_inbox_returns_demo_account_and_consistent_global_summary(self):
        inbox = self.inbox()
        self.assertEqual(inbox["account"]["mode"], "demo")
        self.assertFalse(inbox["account"]["connected"])
        self.assertEqual(inbox["account"]["provider"], "Simulated mailbox")
        self.assertEqual(inbox["account"]["email"], "alex.morgan@example.com")
        self.assertGreaterEqual(len(inbox["messages"]), 24)
        self.assertEqual(inbox["summary"]["total"], len(inbox["messages"]))
        self.assertEqual(len(inbox["categories"]), 8)
        self.assertEqual(inbox["priorities"], ["critical", "high", "normal", "low"])
        filtered = self.inbox(category="work", priority="high")
        self.assertEqual(filtered["summary"], inbox["summary"])
        self.assertTrue(all(row["category"] == "work" and row["priority"] == "high" for row in filtered["messages"]))

    def test_reader_and_real_mutations_persist_and_change_views(self):
        message = self.inbox()["messages"][0]
        status, _, loaded = self.json_request("GET", "/api/messages/" + message["id"])
        self.assertEqual(status, 200)
        self.assertEqual(loaded, message)
        path = "/api/messages/" + message["id"]
        status, _, changed = self.json_request("PATCH", path, {"unread": False, "starred": True, "category": "personal", "priority": "low"})
        self.assertEqual(status, 200)
        self.assertFalse(changed["unread"])
        self.assertTrue(changed["starred"])
        self.assertTrue(changed["overridden"])
        self.assertEqual(changed["category"], "personal")
        self.assertEqual(changed["priority"], "low")
        self.assertIn(message["id"], {row["id"] for row in self.inbox(view="starred")["messages"]})
        self.assertNotIn(message["id"], {row["id"] for row in self.inbox(view="unread")["messages"]})
        status, _, archived = self.json_request("PATCH", path, {"archived": True})
        self.assertEqual(status, 200)
        self.assertTrue(archived["archived"])
        self.assertNotIn(message["id"], {row["id"] for row in self.inbox()["messages"]})
        self.assertIn(message["id"], {row["id"] for row in self.inbox(view="archived")["messages"]})
        status, _, restored = self.json_request("PATCH", path, {"archived": False})
        self.assertEqual(status, 200)
        self.assertFalse(restored["archived"])
        self.assertEqual(self.json_request("GET", path)[2], restored)

    def test_raw_import_search_and_repeated_import(self):
        before = self.inbox()["summary"]["total"]
        status, headers, body = self.request("POST", "/api/import", IMPORT, {"Content-Type": "message/rfc822"})
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        imported = json.loads(body)["message"]
        self.assertEqual(imported["subject"], "HTTP violet project approval")
        self.assertEqual(imported["source"], "import")
        self.assertEqual(imported["sender"]["address"], "http.sender@example.com")
        self.assertEqual([row["id"] for row in self.inbox(search="VIOLET")["messages"]], [imported["id"]])
        second_status, _, second_body = self.request("POST", "/api/import", IMPORT, {"Content-Type": "application/octet-stream"})
        self.assertEqual(second_status, 200)
        self.assertEqual(json.loads(second_body)["message"]["id"], imported["id"])
        self.assertEqual(self.inbox()["summary"]["total"], before + 1)

    def test_sync_exhaustion_and_reset_are_real_actions(self):
        before = self.inbox()["summary"]["total"]
        total_added = 0
        for remaining in [2, 1, 0]:
            status, _, result = self.json_request("POST", "/api/demo/sync", {})
            self.assertEqual(status, 200)
            self.assertGreater(result["added"], 0)
            self.assertEqual(result["remaining_batches"], remaining)
            total_added += result["added"]
        self.assertEqual(self.inbox()["summary"]["total"], before + total_added)
        status, _, result = self.json_request("POST", "/api/demo/sync")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"added": 0, "remaining_batches": 0})
        status, _, result = self.json_request("POST", "/api/demo/reset", {})
        self.assertEqual(status, 200)
        self.assertEqual(result, {"reset": True})
        self.assertEqual(self.inbox()["summary"]["total"], before)
        self.assertEqual(self.inbox()["summary"]["demo_batches_remaining"], 3)

    def test_invalid_filters_and_updates_return_json_errors(self):
        for filters in [{"category": "unknown"}, {"priority": "urgent"}, {"view": "unknown"}]:
            with self.subTest(filters=filters):
                status, _, error = self.json_request("GET", "/api/inbox?" + urlencode(filters))
                self.assertEqual(status, 400)
                self.assertTrue(error["error"])
        message = self.inbox()["messages"][0]
        for changes in [{"subject": "overwrite"}, {"unread": "yes"}, {"priority": "urgent"}, ["unread"]]:
            with self.subTest(changes=changes):
                status, _, error = self.json_request("PATCH", "/api/messages/" + message["id"], changes)
                self.assertEqual(status, 400)
                self.assertTrue(error["error"])

    def test_missing_paths_and_ids_return_404(self):
        for method, path, payload in [("GET", "/api/messages/not-found", None), ("PATCH", "/api/messages/not-found", {"unread": False}), ("GET", "/not-a-file", None), ("GET", "/api/not-an-api", None)]:
            with self.subTest(method=method, path=path):
                status, _, error = self.json_request(method, path, payload)
                self.assertEqual(status, 404)
                self.assertTrue(error["error"])

    def test_content_types_malformed_json_and_empty_requests(self):
        path = "/api/messages/" + self.inbox()["messages"][0]["id"]
        for method, target, body, headers in [
            ("PATCH", path, b'{"unread":false}', {"Content-Type": "text/plain"}),
            ("POST", "/api/import", IMPORT, {"Content-Type": "application/json"}),
            ("POST", "/api/demo/sync", b"{}", {"Content-Type": "text/plain"}),
        ]:
            with self.subTest(target=target):
                status, _, raw = self.request(method, target, body, headers)
                self.assertEqual(status, 415)
                self.assertTrue(json.loads(raw)["error"])
        status, _, body = self.request("PATCH", path, b"{not json", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertTrue(json.loads(body)["error"])
        status, _, _ = self.request("PATCH", path, b'{"unread":false}', {"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200)
        status, _, body = self.request("POST", "/api/demo/sync", b"")
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(body)["added"], 0)

    def test_oversized_declared_import_is_rejected_before_body_read(self):
        status, _, raw = self.request("POST", "/api/import", b"", {"Content-Type": "message/rfc822", "Content-Length": str(MAX_EMAIL_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertTrue(json.loads(raw)["error"])

    def test_cross_origin_mutations_are_rejected_and_same_origin_works(self):
        message_id = self.inbox()["messages"][0]["id"]
        for method, path, payload in [("PATCH", "/api/messages/" + message_id, {"starred": True}), ("POST", "/api/demo/sync", {}), ("POST", "/api/demo/reset", {})]:
            with self.subTest(method=method, path=path):
                status, headers, error = self.json_request(method, path, payload, {"Origin": "https://evil.example"})
                self.assertEqual(status, 403)
                self.assertTrue(error["error"])
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, body = self.request("POST", "/api/import", IMPORT, {"Content-Type": "message/rfc822", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertTrue(json.loads(body)["error"])
        for browser_headers in [{"Origin": "null"}, {"Origin": f"ftp://{self.host}:{self.port}"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(browser_headers=browser_headers):
                status, _, error = self.json_request("POST", "/api/demo/sync", {}, browser_headers)
                self.assertEqual(status, 403)
                self.assertTrue(error["error"])
        status, _, _ = self.json_request("PATCH", "/api/messages/" + message_id, {"starred": True}, {"Origin": f"http://{self.host}:{self.port}"})
        self.assertEqual(status, 200)

    def test_local_assets_and_no_cache_for_html_and_api(self):
        for path, mime in [("/", "text/html"), ("/app.js", "javascript"), ("/styles.css", "text/css")]:
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(mime, headers.get("Content-Type", ""))
                self.assertTrue(body)
                if path == "/":
                    self.assertIn(b"Inbox Atlas", body)
                    self.assertIn("no-store", headers.get("Cache-Control", ""))
        status, headers, _ = self.json_request("GET", "/api/inbox")
        self.assertEqual(status, 200)
        self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_traversal_and_source_paths_do_not_expose_files(self):
        for path in ["/../pyproject.toml", "/%2e%2e/pyproject.toml", "/src/email_parser/server.py", "/%2e%2e%2f%2e%2e%2fetc/passwd", "/.git/config"]:
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertIn(status, {400, 403, 404})
                self.assertTrue(json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
