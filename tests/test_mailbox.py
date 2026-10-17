"""No-network Gmail backfill, history, isolation and worker lifecycle tests."""

import copy
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from email_parser.gmail import GmailAuthError, GmailError, GmailHistoryExpired, GmailMessageMissing
from email_parser.mailbox import MailboxService
from email_parser.store import InboxStore


NOW = datetime(2026, 10, 17, 12, 30, 15, 123000, timezone.utc)
SINCE = NOW - timedelta(hours=72)


def remote(message_id, instant=None, labels=None):
    return {
        "id": message_id, "thread_id": "thread-" + message_id,
        "raw": ("From: Sender <sender@example.com>\r\nTo: reader@example.com\r\n"
                "Date: Thu, 01 Jan 1970 00:00:00 +0000\r\n"
                f"Subject: Message {message_id}\r\n\r\nA private message body.\r\n").encode(),
        "internal_date": instant or NOW - timedelta(hours=1),
        "labels": ["INBOX", "UNREAD"] if labels is None else labels,
    }


class FakeAuth:
    def __init__(self):
        self.calls = []
        self.error = None

    def access_token(self, account_id):
        self.calls.append(account_id)
        if self.error:
            raise self.error
        return "private-access-token"


class FakeGmail:
    def __init__(self):
        self.messages = {}
        self.pages = {None: {"messages": []}}
        self.history_pages = {None: {"historyId": "200"}}
        self.calls = []
        self.profile_id = "100"
        self.fail_ids = set()
        self.history_error = None
        self.list_error = None
        self.before_list = None
        self.before_get = None
        self.block_id = None
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()

    def __call__(self, token):
        if token != "private-access-token":
            raise AssertionError("Unexpected injected access token")
        self.closed.clear()
        return self

    def profile(self):
        self.calls.append(("profile",))
        return {"emailAddress": "reader@example.com", "historyId": self.profile_id}

    def list_messages(self, after, before=None, page_token=None):
        self.calls.append(("list", after, before, page_token))
        if self.before_list:
            self.before_list()
        if self.list_error:
            raise self.list_error
        return copy.deepcopy(self.pages[page_token])

    def get_message(self, message_id):
        self.calls.append(("get", message_id))
        if self.before_get:
            self.before_get()
        if self.block_id == message_id:
            self.blocked.set()
            if not self.release.wait(4):
                raise RuntimeError("The fake Gmail request was not released")
        if message_id in self.fail_ids:
            raise RuntimeError("private-access-token private email body do not expose")
        if message_id not in self.messages:
            raise GmailMessageMissing("This Gmail message is no longer available.", status=404)
        return copy.deepcopy(self.messages[message_id])

    def history(self, history_id, page_token=None):
        self.calls.append(("history", history_id, page_token))
        if self.history_error:
            raise self.history_error
        return copy.deepcopy(self.history_pages[page_token])

    def close(self):
        self.closed.set()


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = InboxStore(Path(self.temporary.name) / "inbox.sqlite3")
        self.auth = FakeAuth()
        self.gmail = FakeGmail()
        self.notifications = []

    def account(self, email="reader@example.com", *, anchor="100"):
        return self.store.upsert_gmail_account(
            {"emailAddress": email, "historyId": anchor}, "encrypted-private-token", now=NOW,
        )

    def service(self, **options):
        options.setdefault("interval", 3600)
        options.setdefault("on_messages", lambda: self.notifications.append("new"))
        service = MailboxService(self.store, self.auth, client_factory=self.gmail, **options)
        self.addCleanup(service.close)
        self.addCleanup(self.gmail.release.set)
        return service

    def until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.005)
        self.fail("The fake mailbox operation did not complete")

    def imported(self, account_id):
        return (self.store.list_messages(account_id=account_id, view="inbox")
                + self.store.list_messages(account_id=account_id, view="archived"))

    def finish_initial(self, service, account):
        self.until(lambda: self.store.get_account(account["id"])["initial_import_complete"])
        self.until(lambda: service.status(account["id"])["state"] == "idle")

    def enqueue_done(self, service, account_id):
        service.enqueue(account_id)
        self.until(lambda: service.status(account_id)["state"] in {"idle", "error", "disconnected"})

    def test_initial_exact_72_hours_uses_internal_date_and_all_pages(self):
        account = self.account()
        self.gmail.messages = {
            "low": remote("low", SINCE), "mid": remote("mid"), "high": remote("high", NOW),
            "old": remote("old", SINCE - timedelta(microseconds=1)),
            "future": remote("future", NOW + timedelta(microseconds=1)),
            **{label: remote(label, labels=["INBOX", label]) for label in ("SENT", "DRAFT", "SPAM", "TRASH")},
        }
        self.gmail.pages = {
            None: {"messages": [{"id": key} for key in ("low", "old", "SENT", "DRAFT")], "nextPageToken": "next"},
            "next": {"messages": [{"id": key} for key in ("mid", "high", "future", "SPAM", "TRASH")]},
        }
        service = self.service()
        self.finish_initial(service, account)
        messages = self.imported(account["id"])
        self.assertEqual({item["remote_id"] for item in messages}, {"low", "mid", "high"})
        low = next(item for item in messages if item["remote_id"] == "low")
        self.assertEqual(datetime.fromisoformat(low["received_at"]), SINCE)
        self.assertEqual([call for call in self.gmail.calls if call[0] == "list"], [
            ("list", SINCE, NOW, None), ("list", SINCE, NOW, "next"),
        ])
        saved = self.store.get_account(account["id"])
        self.assertEqual(saved["history_id"], "100")
        self.assertEqual(service.status(account["id"])["added"], 3)
        self.assertEqual(len(self.notifications), 2)

    def test_missing_initial_anchor_is_persisted_before_first_page(self):
        account = self.account()
        self.store.update_account_sync(account["id"], {"initial_history_id": None}, account["generation"])
        checked = []

        def check_anchor():
            current = self.store.get_account(account["id"])
            checked.append(current["initial_history_id"])
            self.assertIsNone(current["history_id"])
            self.assertFalse(current["initial_import_complete"])

        self.gmail.before_list = check_anchor
        service = self.service()
        self.finish_initial(service, account)
        self.assertEqual(checked, ["100"])
        self.assertEqual(self.gmail.calls[0], ("profile",))

    def test_authenticated_anchor_survives_connection_to_worker_delay(self):
        account = self.account(anchor="90")
        self.gmail.profile_id = "100"
        service = self.service()
        self.finish_initial(service, account)
        self.assertEqual(self.store.get_account(account["id"])["history_id"], "90")
        self.assertFalse(any(call[0] == "profile" for call in self.gmail.calls))

    def test_partial_failure_does_not_advance_and_retry_preserves_choices(self):
        account = self.account()
        self.gmail.messages = {key: remote(key) for key in ("a", "b")}
        self.gmail.pages = {None: {"messages": [{"id": "a"}, {"id": "b"}]}}
        self.gmail.fail_ids.add("b")
        service = self.service()
        self.until(lambda: service.status(account["id"])["state"] == "error")
        current = self.store.get_account(account["id"])
        self.assertIsNone(current["history_id"])
        self.assertFalse(current["initial_import_complete"])
        self.assertEqual(current["initial_history_id"], "100")
        message = self.imported(account["id"])[0]
        self.store.update_message(message["id"], {"unread": False, "starred": True, "category": "personal", "priority": "low"})
        self.gmail.fail_ids.clear()
        self.enqueue_done(service, account["id"])
        self.assertEqual(len(self.imported(account["id"])), 2)
        saved = self.store.get_message(message["id"])
        self.assertFalse(saved["unread"])
        self.assertTrue(saved["starred"])
        self.assertEqual((saved["category"], saved["priority"]), ("personal", "low"))
        self.assertTrue(self.store.get_account(account["id"])["initial_import_complete"])

    def test_incremental_pagination_labels_deletes_and_old_incoming_messages(self):
        account = self.account()
        for key in ("existing", "deleted"):
            self.store.upsert_remote_message(account["id"], remote(key), expected_generation=account["generation"])
        self.store.update_account_sync(account["id"], {"initial_import_complete": True, "history_id": "100"}, account["generation"])
        self.gmail.messages = {
            "existing": remote("existing", labels=["STARRED"]),
            "older": remote("older", NOW - timedelta(days=9)),
            "outgoing": remote("outgoing", labels=["SENT"]),
        }
        self.gmail.history_pages = {
            None: {"historyId": "199", "nextPageToken": "next", "history": [{
                "messagesAdded": [{"message": {"id": "older"}}, {"message": {"id": "outgoing"}}],
                "labelsRemoved": [{"message": {"id": "existing"}}],
            }]},
            "next": {"historyId": "200", "history": [{
                "labelsAdded": [{"message": {"id": "existing"}}, {"message": {"id": "older"}}],
                "messagesDeleted": [{"message": {"id": "deleted"}}],
            }]},
        }
        service = self.service()
        self.until(lambda: self.store.get_account(account["id"])["history_id"] == "200")
        self.assertEqual([call for call in self.gmail.calls if call[0] == "history"], [
            ("history", "100", None), ("history", "100", "next"),
        ])
        self.assertEqual(len([call for call in self.gmail.calls if call == ("get", "existing")]), 1)
        all_messages = self.imported(account["id"])
        by_remote = {item["remote_id"]: item for item in all_messages}
        self.assertIn("older", by_remote)
        self.assertNotIn("outgoing", by_remote)
        self.assertFalse(by_remote["existing"]["unread"])
        self.assertTrue(by_remote["existing"]["starred"])
        self.assertTrue(by_remote["deleted"]["remote_deleted"])
        self.assertTrue(by_remote["deleted"]["archived"])
        self.assertEqual(len(self.notifications), 1)

    def test_noop_history_does_not_requeue_ai(self):
        account = self.account()
        self.store.update_account_sync(account["id"], {"initial_import_complete": True, "history_id": "100"}, account["generation"])
        service = self.service()
        self.until(lambda: self.store.get_account(account["id"])["history_id"] == "200")
        self.assertEqual(self.notifications, [])
        self.assertEqual(service.status(account["id"])["processed"], 0)

    def test_history_failure_keeps_cursor_and_retries_without_duplicates(self):
        account = self.account()
        self.store.update_account_sync(account["id"], {"initial_import_complete": True, "history_id": "100"}, account["generation"])
        self.gmail.messages = {key: remote(key) for key in ("a", "b")}
        self.gmail.fail_ids.add("b")
        self.gmail.history_pages = {None: {"historyId": "200", "history": [{
            "messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}],
        }]}}
        service = self.service()
        self.until(lambda: service.status(account["id"])["state"] == "error")
        self.assertEqual(self.store.get_account(account["id"])["history_id"], "100")
        self.gmail.fail_ids.clear()
        self.enqueue_done(service, account["id"])
        self.assertEqual(self.store.get_account(account["id"])["history_id"], "200")
        self.assertEqual(len(self.imported(account["id"])), 2)

    def test_expired_history_recovers_with_overlap_and_anchor_before_scan(self):
        account = self.account()
        last_sync = NOW - timedelta(hours=1)
        self.store.update_account_sync(account["id"], {
            "initial_import_complete": True, "history_id": "10", "last_synced_at": last_sync,
        }, account["generation"])
        original = self.store.upsert_remote_message(account["id"], remote("existing"), expected_generation=account["generation"])["message"]
        self.store.update_message(original["id"], {"unread": False, "starred": True})
        self.gmail.history_error = GmailHistoryExpired("Gmail history expired.", status=404)
        self.gmail.profile_id = "900"
        self.gmail.messages = {key: remote(key) for key in ("existing", "new")}
        self.gmail.pages = {None: {"messages": [{"id": "existing"}, {"id": "new"}]}}
        service = self.service()
        self.until(lambda: self.store.get_account(account["id"])["history_id"] == "900")
        self.assertEqual(self.gmail.calls[:3], [
            ("history", "10", None), ("profile",),
            ("list", last_sync - timedelta(minutes=5), None, None),
        ])
        self.assertFalse(self.store.get_message(original["id"])["unread"])
        self.assertTrue(self.store.get_message(original["id"])["starred"])
        self.assertEqual(len(self.imported(account["id"])), 2)

    def test_expired_history_without_success_uses_initial_since(self):
        account = self.account()
        self.store.update_account_sync(account["id"], {"initial_import_complete": True, "history_id": "10"}, account["generation"])
        self.gmail.history_error = GmailHistoryExpired("Gmail history expired.", status=404)
        service = self.service()
        self.until(lambda: self.store.get_account(account["id"])["history_id"] == "100")
        self.assertIn(("list", SINCE, None, None), self.gmail.calls)

    def test_disconnect_during_fetch_rejects_stale_message_and_cursor(self):
        account = self.account()
        self.gmail.messages = {"a": remote("a")}
        self.gmail.pages = {None: {"messages": [{"id": "a"}]}}
        self.gmail.block_id = "a"
        service = self.service()
        self.assertTrue(self.gmail.blocked.wait(2))
        self.store.disconnect_account(account["id"])
        self.gmail.release.set()
        self.until(self.gmail.closed.is_set)
        self.assertEqual(self.imported(account["id"]), [])
        self.assertIsNone(self.store.get_account(account["id"])["history_id"])
        self.assertEqual(service.status(account["id"])["state"], "disconnected")

    def test_reconnect_during_fetch_queues_new_generation_and_deduplicates(self):
        account = self.account()
        self.gmail.messages = {"a": remote("a")}
        self.gmail.pages = {None: {"messages": [{"id": "a"}]}}
        self.gmail.block_id = "a"
        service = self.service()
        self.assertTrue(self.gmail.blocked.wait(2))
        renewed = self.account(anchor="200")
        self.assertNotEqual(account["generation"], renewed["generation"])
        service.enqueue(account["id"])
        self.gmail.release.set()
        self.finish_initial(service, account)
        self.assertEqual(len(self.imported(account["id"])), 1)
        self.assertEqual(self.store.get_account(account["id"])["history_id"], "100")

    def test_ownership_prevents_duplicate_pollers_and_releases_on_close(self):
        service = self.service()
        with self.assertRaisesRegex(ValueError, "Another inbox server"):
            MailboxService(self.store, self.auth, client_factory=self.gmail)
        service.close()
        replacement = self.service()
        self.assertIsNotNone(replacement)

    def test_shutdown_during_scheduler_scan_releases_ownership_before_close_returns(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()

        class PausedScheduler(MailboxService):
            def _schedule_due(inner_self):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("The fake scheduler was not released")
                super()._schedule_due()

        service = PausedScheduler(self.store, self.auth, client_factory=self.gmail)
        self.addCleanup(service.close)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(1))

        def close_service():
            service.close()
            closed.set()

        closer = threading.Thread(target=close_service)
        closer.start()
        self.addCleanup(lambda: closer.join(2))
        self.assertTrue(service._stopped.wait(1))
        # This resumes the scheduler directly into _wake.clear(), after close
        # has already set the wake. The stopped flag must prevent another wait.
        release.set()
        self.assertTrue(closed.wait(0.5), "Shutdown lost its wake while clearing the scheduler event")
        self.assertFalse(service._thread.is_alive())
        replacement = self.service()
        self.assertIsNotNone(replacement)

    def test_network_request_does_not_hold_store_lock(self):
        account = self.account()
        self.gmail.messages = {"a": remote("a")}
        self.gmail.pages = {None: {"messages": [{"id": "a"}]}}
        wrote = threading.Event()

        def before_get():
            thread = threading.Thread(target=lambda: (self.store.set_preferences("demo", {"search": "still usable"}), wrote.set()))
            thread.start()
            if not wrote.wait(1):
                raise RuntimeError("The network request held the database lock")
            thread.join(1)

        self.gmail.before_get = before_get
        service = self.service()
        self.finish_initial(service, account)
        self.assertTrue(wrote.is_set())

    def test_automatic_poll_imports_new_history_after_initial_sync(self):
        account = self.account()
        service = self.service(interval=0.03)
        self.finish_initial(service, account)
        self.gmail.messages["later"] = remote("later", NOW + timedelta(hours=1))
        self.gmail.history_pages = {None: {"historyId": "300", "history": [{
            "messagesAdded": [{"message": {"id": "later"}}],
        }]}}
        self.until(lambda: len(self.imported(account["id"])) == 1)
        self.until(lambda: self.store.get_account(account["id"])["history_id"] == "300")
        self.assertEqual(self.imported(account["id"])[0]["remote_id"], "later")

    def test_authorization_failure_pauses_automatic_requests_until_retry(self):
        account = self.account()
        self.auth.error = GmailAuthError("Reconnect Gmail to continue.")
        service = self.service(interval=0.02)
        self.until(lambda: self.store.get_account(account["id"])["status"] == "needs_reauth")
        calls = len(self.auth.calls)
        # Use a bounded event wait, longer than several poll periods.
        threading.Event().wait(0.09)
        self.assertEqual(len(self.auth.calls), calls)
        self.auth.error = None
        self.enqueue_done(service, account["id"])
        self.assertTrue(self.store.get_account(account["id"])["initial_import_complete"])

    def test_generic_transport_failure_never_exposes_private_details(self):
        account = self.account()
        self.gmail.messages = {"a": remote("a")}
        self.gmail.pages = {None: {"messages": [{"id": "a"}]}}
        self.gmail.fail_ids.add("a")
        service = self.service()
        self.until(lambda: service.status(account["id"])["state"] == "error")
        public = service.status(account["id"])
        self.assertNotIn("private", str(public))
        self.assertNotIn("token", str(public))
        self.assertEqual(public["error"], "Gmail synchronization failed. Try syncing again.")

    def test_message_deleted_between_listing_and_download_is_not_fatal(self):
        account = self.account()
        self.gmail.pages = {None: {"messages": [{"id": "gone"}]}}
        service = self.service()
        self.finish_initial(service, account)
        self.assertEqual(self.imported(account["id"]), [])
        self.assertEqual(service.status(account["id"])["state"], "idle")

    def test_reconnect_after_restart_resumes_history_without_refetching_window(self):
        account = self.account()
        service = self.service()
        self.finish_initial(service, account)
        service.close()
        renewed = self.store.upsert_gmail_account({"emailAddress": "reader@example.com", "historyId": "999"}, "new-encrypted", now=NOW + timedelta(days=5))
        self.gmail.calls.clear()
        replacement = self.service()
        self.until(lambda: self.store.get_account(account["id"])["history_id"] == "200")
        self.assertEqual(renewed["initial_since"], account["initial_since"])
        self.assertEqual(self.gmail.calls, [("history", "100", None)])
        self.assertEqual(replacement.status(account["id"])["state"], "idle")

    def test_disabled_service_does_not_contact_google_or_take_ownership(self):
        account = self.account()
        first = self.service(enabled=False)
        second = self.service(enabled=False)
        self.assertEqual(first.enqueue(account["id"])["state"], "idle")
        self.assertEqual(second.status(account["id"])["state"], "idle")
        self.assertEqual(self.auth.calls, [])
        self.assertEqual(self.gmail.calls, [])

    def test_label_changes_on_existing_messages_do_not_reclassify(self):
        account = self.account()
        result = self.store.upsert_remote_message(account["id"], remote("a"), expected_generation=account["generation"])
        self.store.update_message(result["message"]["id"], {"unread": False, "archived": False, "starred": False})
        self.store.update_account_sync(account["id"], {"initial_import_complete": True, "history_id": "100"}, account["generation"])
        self.gmail.messages = {"a": remote("a", labels=["UNREAD", "STARRED"])}
        self.gmail.history_pages = {None: {"historyId": "200", "history": [{
            "labelsAdded": [{"message": {"id": "a"}}],
        }]}}
        service = self.service()
        self.until(lambda: self.store.get_account(account["id"])["history_id"] == "200")
        saved = self.store.get_message(result["message"]["id"])
        self.assertEqual((saved["unread"], saved["archived"], saved["starred"]), (False, False, False))
        self.assertEqual(self.notifications, [])

    def test_invalid_repeated_page_token_does_not_commit_cursor(self):
        account = self.account()
        self.gmail.pages = {None: {"nextPageToken": "same"}, "same": {"nextPageToken": "same"}}
        service = self.service()
        self.until(lambda: service.status(account["id"])["state"] == "error")
        self.assertIsNone(self.store.get_account(account["id"])["history_id"])
        self.assertEqual(len([call for call in self.gmail.calls if call[0] == "list"]), 2)

    def test_transient_failure_is_retried_on_next_scheduled_poll(self):
        account = self.account()
        self.gmail.list_error = GmailError("Google is temporarily unavailable.")
        service = self.service(interval=0.08)
        self.until(lambda: service.status(account["id"])["state"] == "error")
        self.gmail.list_error = None
        self.finish_initial(service, account)
        self.assertGreaterEqual(len([call for call in self.gmail.calls if call[0] == "list"]), 2)

    def test_new_arrival_during_backfill_is_recovered_from_saved_anchor(self):
        account = self.account(anchor="90")
        self.gmail.messages = {"recent": remote("recent")}
        self.gmail.pages = {None: {"messages": [{"id": "recent"}]}}

        def arrive_during_scan():
            self.gmail.messages["arrival"] = remote("arrival", NOW + timedelta(seconds=5))
            self.gmail.history_pages = {None: {"historyId": "105", "history": [{
                "messagesAdded": [{"message": {"id": "arrival"}}],
            }]}}

        self.gmail.before_list = arrive_during_scan
        service = self.service()
        self.finish_initial(service, account)
        self.assertEqual({item["remote_id"] for item in self.imported(account["id"])}, {"recent"})
        self.enqueue_done(service, account["id"])
        self.assertEqual({item["remote_id"] for item in self.imported(account["id"])}, {"recent", "arrival"})
        self.assertIn(("history", "90", None), self.gmail.calls)

    def test_distinct_accounts_keep_same_provider_ids_and_local_choices_separate(self):
        first = self.account("first@example.com")
        second = self.account("second@example.com")
        self.gmail.messages = {"same": remote("same")}
        self.gmail.pages = {None: {"messages": [{"id": "same"}]}}
        service = self.service()
        self.finish_initial(service, first)
        self.finish_initial(service, second)
        first_message = self.imported(first["id"])[0]
        second_message = self.imported(second["id"])[0]
        self.assertNotEqual(first_message["id"], second_message["id"])
        self.store.update_message(first_message["id"], {"unread": False, "starred": True})
        self.assertTrue(self.store.get_message(second_message["id"])["unread"])
        self.assertFalse(self.store.get_message(second_message["id"])["starred"])
        self.assertEqual(set(self.auth.calls), {first["id"], second["id"]})
        self.assertTrue(all(item["account_id"] == first["id"] for item in self.imported(first["id"])))

    def test_backfill_does_not_cap_at_first_500_messages(self):
        account = self.account()
        ids = [f"mail-{number}" for number in range(503)]
        self.gmail.messages = {key: remote(key) for key in ids}
        self.gmail.pages = {
            None: {"messages": [{"id": key} for key in ids[:500]], "nextPageToken": "remaining"},
            "remaining": {"messages": [{"id": key} for key in ids[500:]]},
        }
        service = self.service()
        self.finish_initial(service, account)
        self.assertEqual(len(self.imported(account["id"])), 503)
        self.assertEqual(service.status(account["id"])["added"], 503)

    def test_close_during_request_keeps_ownership_until_exit_and_cancels_writes(self):
        account = self.account()
        self.gmail.messages = {"a": remote("a")}
        self.gmail.pages = {None: {"messages": [{"id": "a"}]}}
        self.gmail.block_id = "a"
        service = self.service()
        self.assertTrue(self.gmail.blocked.wait(2))
        service.close()
        with self.assertRaisesRegex(ValueError, "Another inbox server"):
            MailboxService(self.store, self.auth, client_factory=self.gmail)
        self.gmail.release.set()
        self.until(lambda: not service._thread.is_alive())
        self.assertEqual(self.imported(account["id"]), [])
        self.assertIsNone(self.store.get_account(account["id"])["history_id"])

    def test_incremental_excluded_message_can_return_without_losing_local_choices(self):
        account = self.account()
        result = self.store.upsert_remote_message(account["id"], remote("a"), expected_generation=account["generation"])
        self.store.update_message(result["message"]["id"], {"unread": False})
        self.store.update_account_sync(account["id"], {"initial_import_complete": True, "history_id": "100"}, account["generation"])
        self.gmail.messages = {"a": remote("a", labels=["SPAM"])}
        self.gmail.history_pages = {None: {"historyId": "200", "history": [{
            "labelsAdded": [{"message": {"id": "a"}}],
        }]}}
        service = self.service()
        self.until(lambda: self.store.get_account(account["id"])["history_id"] == "200")
        self.assertTrue(self.store.get_message(result["message"]["id"])["remote_deleted"])
        self.gmail.messages["a"] = remote("a", labels=["INBOX", "UNREAD"])
        self.gmail.history_pages[None]["historyId"] = "300"
        self.enqueue_done(service, account["id"])
        saved = self.store.get_message(result["message"]["id"])
        self.assertFalse(saved["remote_deleted"])
        self.assertFalse(saved["archived"])
        self.assertFalse(saved["unread"])
        self.assertEqual(self.notifications, [])

    def test_delayed_backfill_history_expiry_recovers_entire_uncovered_interval(self):
        connected_at = datetime.now(timezone.utc) - timedelta(days=10)
        account = self.store.upsert_gmail_account(
            {"emailAddress": "reader@example.com", "historyId": "90"},
            "encrypted-private-token", now=connected_at,
        )
        service = self.service()
        self.finish_initial(service, account)
        self.assertEqual(self.store.get_account(account["id"])["last_synced_at"], connected_at.isoformat())

        # The frozen initial window excludes this message. Its expired history
        # anchor must trigger recovery across all ten elapsed days, rather than
        # starting five minutes before this worker happened to start.
        self.gmail.messages = {"gap": remote("gap", connected_at + timedelta(days=1))}
        self.gmail.pages = {None: {"messages": [{"id": "gap"}]}}
        self.gmail.history_error = GmailHistoryExpired("Gmail history expired.", status=404)
        self.gmail.profile_id = "900"
        catchup_started = datetime.now(timezone.utc)
        self.enqueue_done(service, account["id"])
        self.assertIn(("list", connected_at - timedelta(minutes=5), None, None), self.gmail.calls)
        self.assertEqual([item["remote_id"] for item in self.imported(account["id"])], ["gap"])
        saved = self.store.get_account(account["id"])
        self.assertEqual(saved["history_id"], "900")
        self.assertGreaterEqual(datetime.fromisoformat(saved["last_synced_at"]), catchup_started)

    def test_partial_initial_page_notifies_ai_before_failed_download_then_404(self):
        account = self.account()
        self.gmail.messages = {key: remote(key) for key in ("a", "b")}
        self.gmail.pages = {None: {"messages": [{"id": "a"}, {"id": "b"}]}}
        self.gmail.fail_ids.add("b")
        service = self.service()
        self.until(lambda: service.status(account["id"])["state"] == "error")
        self.assertEqual(self.notifications, ["new"])
        self.assertIsNone(self.store.get_account(account["id"])["history_id"])

        # No new inserts occur on retry: a is already durable and b vanished.
        self.gmail.fail_ids.clear()
        del self.gmail.messages["b"]
        self.enqueue_done(service, account["id"])
        self.assertEqual([item["remote_id"] for item in self.imported(account["id"])], ["a"])
        self.assertEqual(self.notifications, ["new"])
        self.assertTrue(self.store.get_account(account["id"])["initial_import_complete"])

    def test_partial_history_notifies_ai_before_failed_download_then_404(self):
        account = self.account()
        self.store.update_account_sync(account["id"], {
            "initial_import_complete": True, "history_id": "100",
        }, account["generation"])
        self.gmail.messages = {key: remote(key) for key in ("a", "b")}
        self.gmail.history_pages = {None: {"historyId": "200", "history": [{
            "messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}],
        }]}}
        self.gmail.fail_ids.add("b")
        service = self.service()
        self.until(lambda: service.status(account["id"])["state"] == "error")
        self.assertEqual(self.notifications, ["new"])
        self.assertEqual(self.store.get_account(account["id"])["history_id"], "100")
        self.gmail.fail_ids.clear()
        del self.gmail.messages["b"]
        self.enqueue_done(service, account["id"])
        self.assertEqual([item["remote_id"] for item in self.imported(account["id"])], ["a"])
        self.assertEqual(self.notifications, ["new"])
        self.assertEqual(self.store.get_account(account["id"])["history_id"], "200")


if __name__ == "__main__":
    unittest.main()
