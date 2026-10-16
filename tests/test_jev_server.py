"""Jev HTTP workflows with deterministic local fakes and no external calls."""

import copy
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from email_parser.jev import JevError
from email_parser.server import create_server


class FakeClassifier:
    model = "jev-test-model"

    def __init__(self, *, fail_first=False):
        self._lock = threading.Lock()
        self._next_block = None
        self._blocks = []
        self._fail_next = fail_first
        self._calls = []
        self.closed = False

    @property
    def calls(self):
        with self._lock:
            return copy.deepcopy(self._calls)

    def block_next(self):
        entered, release = threading.Event(), threading.Event()
        with self._lock:
            self._next_block = (entered, release)
            self._blocks.append(release)
        return entered, release

    def classify(self, message):
        with self._lock:
            self._calls.append(copy.deepcopy(message))
            block, self._next_block = self._next_block, None
            fail, self._fail_next = self._fail_next, False
        if block:
            entered, release = block
            entered.set()
            if not release.wait(5):
                raise JevError("The local test classifier timed out.")
        if fail:
            raise JevError("Jev is temporarily unavailable. Try again.")
        return {
            "category": "finance", "priority": "high", "score": 75,
            "labels": ["Finance", "Jev AI"],
            "reasons": ["Application rubric: actionable financial correspondence."],
            "classification": {
                "provider": "jev", "status": "complete", "model": self.model,
                "category_confidence": 0.91, "priority_confidence": 0.88,
                "priority_level": 3.0, "assessed_at": datetime.now(timezone.utc).isoformat(),
                "rubric_version": "jev-inbox-v1",
            },
        }

    def close(self):
        with self._lock:
            self.closed = True
            for release in self._blocks:
                release.set()


class JevServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def start_server(self, **options):
        self.server = create_server("127.0.0.1", 0, Path(self.temporary.name) / "inbox.sqlite3", **options)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            self.assertIn("application/json", response.getheader("Content-Type", ""))
            return response.status, json.loads(data)
        finally:
            connection.close()

    def json_request(self, method, path, payload=None, headers=None):
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else None
        return self.request(method, path, body, request_headers)

    def inbox(self):
        status, inbox = self.request("GET", "/api/inbox")
        self.assertEqual(status, 200)
        return inbox

    def wait_inbox(self, predicate, *, timeout=5):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.inbox()
            if predicate(last):
                return last
            time.sleep(0.01)
        self.fail(f"Classification did not reach the expected state: {last['classifier'] if last else None}")

    def wait_ready(self):
        return self.wait_inbox(lambda inbox: (
            inbox["classifier"]["status"] == "ready"
            and inbox["classifier"]["pending"] == 0
            and all(row["classification"]["status"] in {"complete", "manual"} for row in inbox["messages"])
        ))

    def import_message(self, subject):
        raw = (
            "From: Integration Sender <integration@example.com>\r\n"
            "To: Alex Morgan <alex.morgan@example.com>\r\n"
            f"Subject: {subject}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "Please review the project report.\r\n"
        ).encode()
        status, result = self.request("POST", "/api/import", raw, {"Content-Type": "message/rfc822"})
        self.assertEqual(status, 200)
        return result["message"]

    def test_factory_does_not_discover_environment_credentials_by_default(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-placeholder-not-a-real-key"}), patch(
            "email_parser.server.from_environment", side_effect=AssertionError("Unexpected credential discovery")
        ) as discovery:
            self.start_server()
            inbox = self.inbox()
        discovery.assert_not_called()
        self.assertFalse(inbox["classifier"]["configured"])
        self.assertEqual(inbox["classifier"]["status"], "needs_key")
        self.assertTrue(all(row["classification"]["provider"] == "rules" and row["classification"]["status"] == "preview" for row in inbox["messages"]))

    def test_missing_key_exposes_preview_and_classification_actions_return_409(self):
        with patch("email_parser.server.from_environment", return_value=None) as discovery:
            self.start_server(enable_ai=True)
        discovery.assert_called_once_with()
        inbox = self.inbox()
        self.assertEqual(inbox["classifier"]["status"], "needs_key")
        self.assertFalse(inbox["classifier"]["configured"])
        self.assertEqual(inbox["classifier"]["complete"], 0)
        for path in ["/api/classification/run", "/api/classification/retry"]:
            with self.subTest(path=path):
                status, error = self.json_request("POST", path, {})
                self.assertEqual(status, 409)
                self.assertTrue(error["error"])
        imported = self.import_message("No-key preview")
        self.assertEqual(imported["classification"]["provider"], "rules")
        self.assertEqual(imported["classification"]["status"], "preview")

    def test_injected_classifier_processes_seed_without_blocking_http_reads(self):
        classifier = FakeClassifier()
        entered, release = classifier.block_next()
        self.start_server(classifier=classifier)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        while_blocked = self.inbox()
        self.assertTrue(while_blocked["classifier"]["configured"])
        self.assertEqual(while_blocked["classifier"]["status"], "processing")
        self.assertGreater(while_blocked["classifier"]["pending"], 0)
        self.assertGreaterEqual(len(while_blocked["messages"]), 24)
        release.set()
        ready = self.wait_ready()
        progress = ready["classifier"]
        self.assertEqual(progress["model"], classifier.model)
        self.assertEqual(progress["complete"], progress["total"])
        self.assertEqual(progress["failed"], 0)
        self.assertEqual(len(classifier.calls), progress["total"])
        for message in ready["messages"]:
            self.assertEqual((message["category"], message["priority"], message["score"]), ("finance", "high", 75))
            self.assertEqual(message["classification"]["provider"], "jev")
            self.assertEqual(message["classification"]["status"], "complete")

    def test_import_sync_and_reset_enqueue_new_classification_jobs(self):
        classifier = FakeClassifier()
        self.start_server(classifier=classifier)
        initial = self.wait_ready()
        seed_count = initial["classifier"]["total"]
        entered, release = classifier.block_next()
        self.addCleanup(release.set)
        imported = self.import_message("Queued imported message")
        self.assertTrue(entered.wait(2))
        self.assertIn(imported["classification"]["status"], {"pending", "processing"})
        release.set()
        after_import = self.wait_ready()
        imported_id = imported["id"]
        loaded = next(row for row in after_import["messages"] if row["id"] == imported_id)
        self.assertEqual(loaded["classification"]["status"], "complete")
        self.assertEqual(loaded["category"], "finance")
        status, sync = self.json_request("POST", "/api/demo/sync", {})
        self.assertEqual(status, 200)
        after_sync = self.wait_ready()
        self.assertEqual(after_sync["classifier"]["total"], seed_count + 1 + sync["added"])
        self.assertEqual(after_sync["classifier"]["complete"], after_sync["classifier"]["total"])
        calls_before_reset = len(classifier.calls)
        status, result = self.json_request("POST", "/api/demo/reset", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["reset"])
        after_reset = self.wait_ready()
        self.assertEqual(after_reset["classifier"]["total"], seed_count + 1)
        self.assertEqual(len(classifier.calls), calls_before_reset + seed_count)
        self.assertIn(imported_id, {row["id"] for row in after_reset["messages"]})

    def test_manual_changes_during_processing_survive_completion_and_forced_run(self):
        classifier = FakeClassifier()
        self.start_server(classifier=classifier)
        self.wait_ready()
        entered, release = classifier.block_next()
        self.addCleanup(release.set)
        imported = self.import_message("Preserve manual decisions")
        self.assertTrue(entered.wait(2))
        path = "/api/messages/" + imported["id"]
        status, changed = self.json_request("PATCH", path, {"category": "personal", "priority": "low", "unread": False, "starred": True})
        self.assertEqual(status, 200)
        self.assertTrue(changed["overridden"])
        release.set()
        self.wait_ready()
        status, preserved = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual((preserved["category"], preserved["priority"]), ("personal", "low"))
        self.assertFalse(preserved["unread"])
        self.assertTrue(preserved["starred"])
        self.assertEqual(preserved["classification"]["status"], "manual")
        status, _ = self.json_request("POST", "/api/classification/run", {})
        self.assertEqual(status, 200)
        ready = self.wait_ready()
        self.assertEqual(ready["classifier"]["manual"], 1)
        self.assertEqual(self.request("GET", path)[1], preserved)
        self.assertEqual(sum(row["id"] == imported["id"] for row in classifier.calls), 1)

    def test_flag_only_change_during_processing_preserves_flags_and_applies_ai(self):
        classifier = FakeClassifier()
        self.start_server(classifier=classifier)
        self.wait_ready()
        entered, release = classifier.block_next()
        self.addCleanup(release.set)
        imported = self.import_message("Preserve reader flags")
        self.assertTrue(entered.wait(2))
        path = "/api/messages/" + imported["id"]
        status, _ = self.json_request("PATCH", path, {"unread": False, "starred": True, "archived": True})
        self.assertEqual(status, 200)
        release.set()
        self.wait_ready()
        status, result = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertFalse(result["unread"])
        self.assertTrue(result["starred"])
        self.assertTrue(result["archived"])
        self.assertFalse(result["overridden"])
        self.assertEqual(result["classification"]["status"], "complete")
        self.assertEqual((result["category"], result["priority"]), ("finance", "high"))

    def test_provider_failure_pauses_queue_and_retry_finishes_remaining_jobs(self):
        classifier = FakeClassifier(fail_first=True)
        self.start_server(classifier=classifier)
        failed = self.wait_inbox(lambda inbox: (
            inbox["classifier"]["status"] == "error"
            and any(row["classification"]["status"] == "failed" for row in inbox["messages"])
        ))
        self.assertEqual(failed["classifier"]["failed"], 1)
        self.assertTrue(failed["classifier"]["error"])
        self.assertEqual(len(classifier.calls), 1)
        for _ in range(3):
            self.assertEqual(self.inbox()["classifier"]["status"], "error")
        self.assertEqual(len(classifier.calls), 1)
        self.assertTrue(any(row["classification"]["status"] == "failed" for row in failed["messages"]))
        status, progress = self.json_request("POST", "/api/classification/retry", {})
        self.assertEqual(status, 200)
        self.assertTrue(progress["configured"])
        ready = self.wait_ready()
        self.assertEqual(ready["classifier"]["complete"], ready["classifier"]["total"])
        self.assertEqual(ready["classifier"]["failed"], 0)
        self.assertEqual(len(classifier.calls), ready["classifier"]["total"] + 1)

    def test_forced_run_reclassifies_complete_rows_and_empty_body_is_supported(self):
        classifier = FakeClassifier()
        self.start_server(classifier=classifier)
        initial = self.wait_ready()
        before = len(classifier.calls)
        status, progress = self.request("POST", "/api/classification/run", b"")
        self.assertEqual(status, 200)
        self.assertTrue(progress["configured"])
        self.wait_ready()
        self.assertEqual(len(classifier.calls), before + initial["classifier"]["total"])

    def test_ai_action_endpoints_reject_cross_origin_and_bad_bodies(self):
        classifier = FakeClassifier()
        self.start_server(classifier=classifier)
        self.wait_ready()
        before = len(classifier.calls)
        for path in ["/api/classification/run", "/api/classification/retry"]:
            with self.subTest(path=path):
                status, error = self.json_request("POST", path, {}, {"Origin": "https://foreign.example"})
                self.assertEqual(status, 403)
                self.assertTrue(error["error"])
                for payload in [{"unexpected": True}, []]:
                    status, error = self.json_request("POST", path, payload)
                    self.assertEqual(status, 400)
                    self.assertTrue(error["error"])
                status, error = self.request("POST", path, b"{bad", {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
                self.assertTrue(error["error"])
        self.assertEqual(len(classifier.calls), before)


if __name__ == "__main__":
    unittest.main()
