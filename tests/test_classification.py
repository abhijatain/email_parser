"""Deterministic background classification and stale-result protection."""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from email_parser.classification import ClassificationService, RUBRIC_VERSION
from email_parser.store import InboxStore


def decision(message):
    return {
        "category": "personal", "priority": "high", "score": 75,
        "labels": ["Personal", "Jev AI"], "reasons": ["Application priority rubric: high attention."],
        "classification": {"provider": "jev", "status": "complete", "model": "jev-resolved-version",
                           "category_confidence": 0.8, "priority_confidence": 0.7,
                           "priority_level": 3.0, "rubric_version": RUBRIC_VERSION},
        # Extra provider fields must never overwrite local state or message data.
        "unread": False, "subject": "untrusted overwrite",
    }


class FakeClassifier:
    model = "jev-latest"

    def __init__(self, *, block=False, failures=()):
        self.calls = []
        self.failures = set(failures)
        self.block = block
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()

    def classify(self, message):
        self.calls.append(message)
        number = len(self.calls)
        self.entered.set()
        if self.block:
            if not self.release.wait(3):
                raise RuntimeError("Test provider was not released")
        if number in self.failures:
            raise RuntimeError("secret-key-do-not-show and private email content")
        return decision(message)

    def close(self):
        self.closed.set()


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "inbox.sqlite3"
        self.store = InboxStore(self.path)
        # Small real parsed-message fixture keeps concurrency assertions clear.
        self.messages = self.store.list_messages()[:3]
        with self.store._connection(write=True) as connection:
            connection.execute("DELETE FROM messages")
            for message in self.messages:
                self.store._put(connection, message)

    def service(self, classifier=None):
        service = ClassificationService(self.store, classifier)
        self.addCleanup(service.close)
        if classifier is not None:
            self.addCleanup(classifier.release.set)
        return service

    def until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.005)
        self.fail("Background operation did not finish")

    def test_no_key_is_explicit_preview_and_does_not_call_a_provider(self):
        service = self.service()
        status = service.status()
        self.assertEqual(status, {
            "provider": "jev", "model": None, "configured": False, "status": "needs_key",
            "total": 3, "complete": 0, "pending": 3, "failed": 0, "manual": 0, "error": None,
        })
        self.assertEqual(service.enqueue(force=True), status)
        with self.assertRaisesRegex(ValueError, "TYPESAFE_API_KEY"):
            service.retry()
        for message in self.store.list_messages():
            self.assertEqual(message["classification"], {"provider": "rules", "status": "preview"})

    def test_legacy_metadata_migration_preserves_content_and_manual_decisions(self):
        first, second = self.messages[:2]
        with self.store._connection(write=True) as connection:
            for message in (first, second):
                message.pop("classification")
            second["overridden"] = True
            self.store._put(connection, first)
            self.store._put(connection, second)
        self.store = InboxStore(self.path)
        self.assertEqual(self.store.get_message(first["id"])["classification"], {"provider": "rules", "status": "preview"})
        self.assertEqual(self.store.get_message(second["id"])["classification"], {"provider": "manual", "status": "manual"})
        classifier = FakeClassifier()
        service = self.service(classifier)
        self.until(lambda: service.status()["status"] == "ready")
        self.assertEqual(service.status()["manual"], 1)
        self.assertEqual(service.status()["complete"], 2)
        self.assertEqual(len(classifier.calls), 2)
        self.assertEqual(self.store.get_message(second["id"])["category"], second["category"])

    def test_startup_and_import_are_queued_and_completed_alias_is_cached(self):
        classifier = FakeClassifier()
        service = self.service(classifier)
        self.until(lambda: service.status()["status"] == "ready")
        self.assertEqual(len(classifier.calls), 3)
        for message in self.store.list_messages():
            self.assertEqual(message["classification"]["model"], "jev-resolved-version")
            self.assertEqual(message["classification"]["requested_model"], "jev-latest")
        # Force is opt-in; ordinary refresh/import does not spend calls again.
        service.enqueue()
        self.assertEqual(service.status()["complete"], 3)
        data = b"From: sender@example.com\r\nSubject: Fresh message\r\n\r\nHello there."
        imported = self.store.import_email(data)
        self.assertEqual(imported["classification"]["status"], "preview")
        service.enqueue()
        self.until(lambda: service.status()["complete"] == 4)
        self.assertEqual(len(classifier.calls), 4)
        service.close()
        next_classifier = FakeClassifier()
        next_service = self.service(next_classifier)
        self.assertEqual(next_service.status()["status"], "ready")
        self.assertEqual(next_classifier.calls, [])

    def test_changed_model_and_rubric_reclassify_without_losing_flags(self):
        for message in self.messages:
            result = decision(message)
            message.update({key: result[key] for key in ("category", "priority", "score", "labels", "reasons", "classification")})
            message["classification"]["requested_model"] = "jev-older"
            with self.store._connection(write=True) as connection:
                self.store._put(connection, message)
        classifier = FakeClassifier()
        service = self.service(classifier)
        self.until(lambda: service.status()["status"] == "ready")
        self.assertEqual(len(classifier.calls), 3)
        service.close()
        with self.store._connection(write=True) as connection:
            message = self.store._rows(connection)[0]
            message["classification"]["rubric_version"] = "old-rubric"
            self.store._put(connection, message)
        classifier2 = FakeClassifier()
        service2 = self.service(classifier2)
        self.until(lambda: service2.status()["status"] == "ready")
        self.assertEqual(len(classifier2.calls), 1)

    def test_inflight_call_holds_no_store_lock_and_preserves_current_flags(self):
        classifier = FakeClassifier(block=True)
        service = self.service(classifier)
        self.assertTrue(classifier.entered.wait(1))
        working = classifier.calls[0]
        # These operations must finish while the provider is still blocked.
        completed = threading.Event()
        def edit_while_processing():
            self.store.update_message(working["id"], {"unread": True, "starred": True, "archived": True})
            self.store.list_messages()
            self.store.summary()
            service.status()
            completed.set()
        thread = threading.Thread(target=edit_while_processing, daemon=True)
        thread.start()
        self.assertTrue(completed.wait(1), "Remote classification held a store lock")
        classifier.release.set()
        thread.join(1)
        self.until(lambda: service.status()["status"] == "ready")
        result = self.store.get_message(working["id"])
        self.assertTrue(result["unread"])
        self.assertTrue(result["starred"])
        self.assertTrue(result["archived"])
        self.assertEqual(result["subject"], working["subject"])
        self.assertEqual(result["classification"]["provider"], "jev")
        self.assertEqual(service.status()["complete"], 3)

    def test_manual_override_cancels_inflight_result_and_force_respects_it(self):
        classifier = FakeClassifier(block=True)
        service = self.service(classifier)
        self.assertTrue(classifier.entered.wait(1))
        message_id = classifier.calls[0]["id"]
        manual = self.store.update_message(message_id, {"category": "finance", "priority": "low"})
        classifier.release.set()
        self.until(lambda: service.status()["status"] == "ready")
        self.assertEqual(self.store.get_message(message_id), manual)
        self.assertEqual(service.status()["manual"], 1)
        service.enqueue(force=True)
        self.until(lambda: len(classifier.calls) == 5 and service.status()["status"] == "ready")
        self.assertEqual(self.store.get_message(message_id), manual)
        self.assertEqual(sum(message["id"] == message_id for message in classifier.calls), 1)

    def test_reset_discards_response_for_reused_demo_id(self):
        classifier = FakeClassifier(block=True)
        service = self.service(classifier)
        self.assertTrue(classifier.entered.wait(1))
        message_id = classifier.calls[0]["id"]
        self.store.reset_demo()
        reset_message = self.store.get_message(message_id)
        self.assertEqual(reset_message["classification"]["status"], "preview")
        # Do not enqueue the new demo yet: old tokens alone cannot mutate it.
        classifier.release.set()
        self.until(lambda: len(classifier.calls) == 1 and service.status()["pending"] >= 24)
        service.close()
        self.assertEqual(self.store.get_message(message_id), reset_message)

    def test_first_failure_pauses_preserves_partial_progress_and_retry_skips_success(self):
        classifier = FakeClassifier(failures={2})
        service = self.service(classifier)
        self.until(lambda: service.status()["status"] == "error")
        status = service.status()
        self.assertEqual((status["complete"], status["failed"], status["pending"]), (1, 1, 1))
        self.assertEqual(len(classifier.calls), 2)
        self.assertNotIn("secret-key", status["error"])
        failed = self.store.get_message(classifier.calls[1]["id"])
        self.assertEqual(failed["classification"]["provider"], "rules")
        self.assertEqual(failed["classification"]["status"], "failed")
        service.enqueue()
        self.assertEqual(service.status()["status"], "error")
        self.assertEqual(len(classifier.calls), 2)
        service.retry()
        self.until(lambda: service.status()["status"] == "ready")
        self.assertEqual(len(classifier.calls), 4)
        self.assertEqual(sum(row["id"] == classifier.calls[0]["id"] for row in classifier.calls), 1)
        self.assertIsNone(service.status()["error"])

    def test_restarting_does_not_retry_persisted_failure_and_recovers_interruption(self):
        classifier = FakeClassifier(failures={1})
        service = self.service(classifier)
        self.until(lambda: service.status()["status"] == "error")
        service.close()
        again = FakeClassifier()
        service2 = self.service(again)
        self.assertEqual(service2.status()["status"], "error")
        self.assertEqual(again.calls, [])
        service2.retry()
        self.until(lambda: service2.status()["status"] == "ready")
        service2.close()
        # Simulate an interrupted remote call by claiming, without finishing.
        self.store.classification_queue(again.model, RUBRIC_VERSION, force=True)
        claimed = self.store.classification_claim(again.model, RUBRIC_VERSION)
        self.assertIsNotNone(claimed)
        third = FakeClassifier()
        service3 = self.service(third)
        self.until(lambda: service3.status()["status"] == "ready")
        self.assertEqual(len(third.calls), 3)

    def test_close_waits_for_idle_worker_and_never_applies_an_inflight_result(self):
        classifier = FakeClassifier(block=True)
        service = self.service(classifier)
        self.assertTrue(classifier.entered.wait(1))
        message_id = classifier.calls[0]["id"]
        before = self.store.get_message(message_id)
        service.close()
        classifier.release.set()
        self.assertTrue(classifier.closed.wait(1))
        self.assertEqual(self.store.get_message(message_id), before)
        self.assertEqual(len(classifier.calls), 1)

    def test_only_one_worker_can_own_a_database_until_inflight_close_finishes(self):
        first = FakeClassifier(block=True)
        service = self.service(first)
        self.assertTrue(first.entered.wait(1))
        with self.store._connection() as connection:
            tokens = list(connection.execute("SELECT message_id,token FROM classification_jobs ORDER BY message_id"))
        second_store = InboxStore(self.path)
        second = FakeClassifier()
        self.addCleanup(second.close)
        with self.assertRaisesRegex(ValueError, "Another inbox server"):
            ClassificationService(second_store, second)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(second.calls, [])
        with self.store._connection() as connection:
            self.assertEqual(tokens, list(connection.execute("SELECT message_id,token FROM classification_jobs ORDER BY message_id")))
        # Unconfigured readers do not own the paid worker or cancel its job.
        reader = ClassificationService(second_store)
        self.addCleanup(reader.close)
        self.assertEqual(reader.status()["status"], "needs_key")
        service.close()
        with self.assertRaisesRegex(ValueError, "Another inbox server"):
            ClassificationService(second_store, second)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(second.calls, [])
        first.release.set()
        service._thread.join(1)
        self.assertFalse(service._thread.is_alive())
        successor = self.service(second)
        self.until(lambda: successor.status()["status"] == "ready")
        self.assertEqual(len(second.calls), 3)

    def test_failed_construction_releases_ownership_for_the_next_worker(self):
        unused = FakeClassifier()
        self.addCleanup(unused.close)
        with patch.object(self.store, "classification_queue", side_effect=RuntimeError("startup failed")):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                ClassificationService(self.store, unused)
        self.assertEqual(unused.calls, [])
        # A stale file is intentionally harmless: the OS lock was released.
        self.assertTrue(Path(str(self.path.resolve()) + ".jev.lock").exists())
        classifier = FakeClassifier()
        successor = self.service(classifier)
        self.until(lambda: successor.status()["status"] == "ready")
        self.assertEqual(len(classifier.calls), 3)

    def test_process_exit_releases_ownership_and_interrupted_jobs_recover(self):
        script = """
import sys
import threading
from email_parser.classification import ClassificationService
from email_parser.store import InboxStore
class BlockedClassifier:
    model = 'jev-latest'
    def classify(self, message):
        print('provider-request-started', flush=True)
        threading.Event().wait()
    def close(self):
        pass
service = ClassificationService(InboxStore(sys.argv[1]), BlockedClassifier())
threading.Event().wait()
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(self.path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        def clean_process():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        self.addCleanup(clean_process)
        started = threading.Event()
        output = []
        def read_start():
            output.append(process.stdout.readline().strip())
            started.set()
        monitor = threading.Thread(target=read_start, daemon=True)
        monitor.start()
        self.assertTrue(started.wait(3), "Child worker did not start")
        self.assertEqual(output, ["provider-request-started"])
        classifier = FakeClassifier()
        with self.assertRaisesRegex(ValueError, "Another inbox server"):
            ClassificationService(self.store, classifier)
        self.assertEqual(classifier.calls, [])
        # Simulate an abrupt crash with an unfinished processing row. Advisory
        # ownership must release without deleting or trusting a PID lock file.
        process.kill()
        process.wait(timeout=3)
        monitor.join(1)
        successor = self.service(classifier)
        self.until(lambda: successor.status()["status"] == "ready")
        self.assertEqual(len(classifier.calls), 3)


if __name__ == "__main__":
    unittest.main()
