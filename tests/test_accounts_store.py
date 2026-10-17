"""Durable account isolation, local choices, OAuth transactions and migration."""

import os
import stat
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from email_parser.store import InboxStore, account_public


NOW = datetime(2026, 10, 17, 12, 30, tzinfo=timezone.utc)
RAW = (
    b"From: Sender <sender@example.com>\r\nTo: Reader <reader@example.com>\r\n"
    b"Subject: Project review\r\nDate: Mon, 01 Jan 2001 00:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\nPlease review the project.\r\n"
)


def remote(remote_id="provider-1", *, labels=None, raw=RAW, internal_date=NOW):
    return {"id": remote_id, "raw": raw, "internal_date": internal_date,
            "labels": ["INBOX", "UNREAD"] if labels is None else labels,
            "thread_id": "thread-" + remote_id}


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "inbox.sqlite3"
        self.store = InboxStore(self.path)
        self.account = self.store.upsert_gmail_account(
            {"emailAddress": "Reader@Example.com", "historyId": "999"}, "encrypted-token-fixture", NOW,
        )

    def ingest(self, item=None, account=None):
        account = account or self.account
        return self.store.upsert_remote_message(
            account["id"], item or remote(), expected_generation=account["generation"],
        )

    def test_account_defaults_exact_72_hour_window_and_no_token_in_public_or_internal_json(self):
        account = self.account
        self.assertEqual(account["email"], "reader@example.com")
        self.assertEqual(datetime.fromisoformat(account["initial_before"]), NOW)
        self.assertEqual(datetime.fromisoformat(account["initial_since"]), NOW - timedelta(hours=72))
        self.assertIsNone(account["history_id"], "Authenticated profile history is an anchor, not a completed cursor")
        self.assertFalse(account["initial_import_complete"])
        self.assertEqual(self.store.get_account_tokens(account["id"]), "encrypted-token-fixture")
        self.assertNotIn("token_blob", self.store.get_account(account["id"]))
        public = self.store.list_accounts()
        self.assertEqual(public[0]["id"], "demo")
        self.assertEqual({row["provider"] for row in public}, {"demo", "gmail"})
        for row in public:
            for private in ("history_id", "initial_since", "initial_before", "generation", "token_blob"):
                self.assertNotIn(private, row)
        self.assertEqual(account_public({**account, "access_token": "must-never-escape"}), public[1])

    def test_reconnect_preserves_cursor_window_initial_anchor_messages_and_local_decisions(self):
        message = self.ingest()["message"]
        edited = self.store.update_message(message["id"], {"unread": False, "category": "personal", "priority": "low"})
        self.assertTrue(self.store.update_account_sync(self.account["id"], {
            "history_id": "101", "initial_history_id": "100", "initial_import_complete": True,
            "last_synced_at": NOW, "status": "ready",
        }, self.account["generation"]))
        current = self.store.get_account(self.account["id"])
        again = self.store.upsert_gmail_account(
            {"emailAddress": " READER@example.com ", "historyId": "500"}, "new-encrypted-token", NOW + timedelta(days=20),
        )
        self.assertEqual(again["id"], self.account["id"])
        self.assertNotEqual(again["generation"], self.account["generation"])
        for field in ("initial_since", "initial_before", "initial_import_complete", "history_id", "initial_history_id", "last_synced_at"):
            self.assertEqual(again[field], current[field])
        self.assertEqual(self.store.get_message(message["id"]), edited)
        self.assertEqual(self.store.get_account_tokens(again["id"]), "new-encrypted-token")
        self.assertFalse(self.store.set_account_tokens(again["id"], "stale-refresh", self.account["generation"]))
        self.assertFalse(self.store.update_account_sync(again["id"], {"history_id": "stale"}, self.account["generation"]))
        self.assertIsNone(self.ingest())

    def test_same_provider_id_isolated_by_account_and_authoritative_internal_date(self):
        first = self.ingest()["message"]
        second_account = self.store.upsert_gmail_account({"emailAddress": "other@example.com"}, "other-encrypted", NOW)
        second = self.ingest(account=second_account)["message"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["received_at"], NOW.isoformat())
        self.assertEqual(first["source"], "gmail")
        self.assertEqual(first["remote_id"], "provider-1")
        self.assertTrue(first["unread"])
        self.assertFalse(first["archived"])
        self.assertEqual(self.store.list_messages(account_id=self.account["id"]), [first])
        self.assertEqual(self.store.list_messages(account_id=second_account["id"]), [second])
        self.assertIsNone(self.store.get_message(first["id"], account_id=second_account["id"]))
        self.assertIsNone(self.store.update_message(first["id"], {"unread": False}, account_id=second_account["id"]))
        self.assertTrue(self.store.get_message(first["id"])["unread"])
        self.assertEqual(self.store.summary(account_id=self.account["id"])["total"], 1)
        self.assertEqual(self.store.summary(account_id=self.account["id"])["demo_batches_remaining"], 0)

    def test_remote_updates_preserve_each_locally_overridden_flag_and_read_time(self):
        message = self.ingest()["message"]
        changed = self.store.update_message(message["id"], {"unread": False, "starred": True, "archived": False})
        self.assertEqual(changed["local_flags"], {"unread": True, "starred": True, "archived": True})
        self.assertIsNotNone(datetime.fromisoformat(changed["last_read_at"]))
        refreshed = self.ingest(remote(labels=["UNREAD"]))
        self.assertFalse(refreshed["inserted"])
        self.assertFalse(refreshed["message"]["unread"])
        self.assertTrue(refreshed["message"]["starred"])
        self.assertFalse(refreshed["message"]["archived"])
        self.assertEqual(refreshed["message"]["provider_labels"], ["UNREAD"])
        self.assertEqual(refreshed["message"]["last_read_at"], changed["last_read_at"])
        # Explicit Mark unread remains until another explicit opening/read.
        self.store.update_message(message["id"], {"unread": True})
        self.assertTrue(self.ingest(remote(labels=["INBOX"]))["message"]["unread"])

    def test_remote_flags_update_only_fields_without_local_override(self):
        message = self.ingest()["message"]
        self.store.update_message(message["id"], {"unread": True})  # same value still overrides
        refreshed = self.ingest(remote(labels=["STARRED"]))["message"]
        self.assertTrue(refreshed["unread"])
        self.assertTrue(refreshed["starred"])
        self.assertTrue(refreshed["archived"])
        self.assertEqual(refreshed["local_flags"], {"unread": True})

    def test_remote_upsert_does_not_replace_message_snapshot_manual_or_ai_outputs(self):
        message = self.ingest()["message"]
        edited = self.store.update_message(message["id"], {"category": "finance", "priority": "critical"})
        refreshed = self.ingest(remote(raw=RAW.replace(b"Project review", b"Changed remote subject")))["message"]
        for field in ("subject", "text", "category", "priority", "score", "labels", "reasons", "classification", "overridden"):
            self.assertEqual(refreshed[field], edited[field])
        with self.store._connection(write=True) as connection:
            refreshed["classification"] = {"provider": "jev", "status": "complete", "model": "test-model"}
            refreshed["overridden"] = False
            self.store._put(connection, refreshed)
        self.assertEqual(self.ingest()["message"]["classification"], refreshed["classification"])

    def test_remote_tombstone_retains_snapshot_and_respects_local_archive_override(self):
        first = self.ingest()["message"]
        second = self.ingest(remote("provider-2"))["message"]
        self.store.update_message(second["id"], {"archived": False})
        for remote_id in ("provider-1", "provider-2"):
            self.store.mark_remote_deleted(self.account["id"], remote_id, expected_generation=self.account["generation"])
        deleted = self.store.get_message(first["id"])
        self.assertTrue(deleted["remote_deleted"])
        self.assertTrue(deleted["archived"])
        self.assertEqual(deleted["text"], first["text"])
        self.assertFalse(self.store.get_message(second["id"])["archived"])
        self.assertFalse(self.ingest()["message"]["remote_deleted"])

    def test_disconnect_invalidates_all_remote_writes_but_keeps_choices_and_preferences(self):
        message = self.ingest()["message"]
        edited = self.store.update_message(message["id"], {"starred": True, "unread": False, "category": "personal"})
        preferences = self.store.set_preferences(self.account["id"], {"view": "starred", "search": "persist"})
        disconnected = self.store.disconnect_account(self.account["id"])
        self.assertFalse(disconnected["connected"])
        self.assertEqual(disconnected["status"], "disconnected")
        self.assertIsNone(self.store.get_account_tokens(self.account["id"]))
        self.assertEqual(self.store.get_message(message["id"]), edited)
        self.assertEqual(self.store.get_preferences(self.account["id"]), preferences)
        self.assertIsNone(self.ingest(remote("provider-new")))
        self.assertFalse(self.store.set_account_tokens(self.account["id"], "stale", self.account["generation"]))
        self.assertFalse(self.store.update_account_sync(self.account["id"], {"status": "ready"}, self.account["generation"]))
        self.store.mark_remote_deleted(self.account["id"], "provider-1", expected_generation=self.account["generation"])
        self.assertEqual(self.store.get_message(message["id"]), edited)

    def test_preferences_and_active_account_persist_per_account_with_strict_validation(self):
        self.assertEqual(self.store.get_preferences(), {"view": "inbox", "mode": "priority", "category": "", "priority": "", "search": ""})
        preferences = self.store.set_preferences(self.account["id"], {
            "view": "unread", "mode": "all", "category": "work", "priority": "important", "search": "Café",
        })
        self.store.set_active_account_id(self.account["id"])
        reopened = InboxStore(self.path)
        self.assertEqual(reopened.get_active_account_id(), self.account["id"])
        self.assertEqual(reopened.get_preferences(self.account["id"]), preferences)
        self.assertEqual(reopened.get_preferences()["search"], "")
        for changes in ({"unknown": "x"}, {"view": "all"}, {"mode": "bad"}, {"category": "missing"},
                        {"priority": "urgent"}, {"search": "x" * 501}, {"search": False}, [], {"view": None}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.store.set_preferences(self.account["id"], changes)
                self.assertEqual(self.store.get_preferences(self.account["id"]), preferences)
        with self.assertRaises(ValueError):
            self.store.set_active_account_id("missing")

    def test_demo_reset_and_imports_never_change_other_accounts_or_duplicate_imports(self):
        gmail = self.ingest()["message"]
        demo_import = self.store.import_email(RAW)
        gmail_import = self.store.import_email(RAW, account_id=self.account["id"])
        self.assertNotEqual(demo_import["id"], gmail_import["id"])
        self.assertTrue(demo_import["id"].startswith("import-"))
        self.assertEqual(self.store.import_email(RAW, account_id=self.account["id"]), gmail_import)
        self.store.sync_demo()
        self.store.reset_demo()
        self.assertEqual(self.store.get_message(gmail["id"]), gmail)
        self.assertEqual(self.store.get_message(gmail_import["id"]), gmail_import)
        self.assertEqual(self.store.get_message(demo_import["id"]), demo_import)
        self.assertEqual(self.store.classification_counts(None, "rubric", account_id=self.account["id"])["total"], 2)

    def test_auth_transactions_are_browser_bound_expiring_atomic_and_single_use(self):
        self.store.save_auth_state("state", "browser", "encrypted-pending-payload", time.time() + 600)
        self.assertIsNone(self.store.consume_auth_state("state", "wrong-browser"))
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: self.store.consume_auth_state("state", "browser"), range(4)))
        successful = [result for result in results if result is not None]
        self.assertEqual(len(successful), 1)
        self.assertEqual(successful[0]["payload"], "encrypted-pending-payload")
        self.assertIsNone(self.store.consume_auth_state("state", "browser"))
        self.store.save_auth_state("expired", "browser", "encrypted-expired-payload", time.time() - 1)
        self.assertIsNone(self.store.consume_auth_state("expired", "browser"))

    def test_legacy_database_migration_preserves_flags_ai_outputs_ids_and_file_privacy(self):
        legacy = self.store.list_messages()[0]
        legacy = self.store.update_message(legacy["id"], {"unread": False, "starred": True, "category": "personal", "priority": "low"})
        legacy.pop("account_id")
        legacy.pop("local_flags")
        with self.store._connection(write=True) as connection:
            self.store._put(connection, legacy)
            connection.execute("DROP TABLE accounts")
            connection.execute("DROP TABLE preferences")
            connection.execute("DROP TABLE auth_states")
            connection.execute("DELETE FROM state WHERE key='active_account_id'")
        self.path.chmod(0o644)
        reopened = InboxStore(self.path)
        migrated = reopened.get_message(legacy["id"])
        for key, value in legacy.items():
            self.assertEqual(migrated[key], value)
        self.assertEqual(migrated["account_id"], "demo")
        self.assertEqual(migrated["local_flags"], {})
        self.assertEqual(reopened.get_active_account_id(), "demo")
        self.assertEqual(reopened.list_accounts()[0]["id"], "demo")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_concurrent_duplicate_remote_ingestion_inserts_once(self):
        with ThreadPoolExecutor(max_workers=5) as workers:
            results = list(workers.map(lambda _: self.ingest(), range(5)))
        self.assertEqual(sum(item["inserted"] for item in results), 1)
        self.assertEqual(len({item["message"]["id"] for item in results}), 1)
        self.assertEqual(len(self.store.list_messages(account_id=self.account["id"])), 1)

    def test_oauth_upsert_generation_commit_rejects_disconnect_and_competing_reconnect(self):
        before = self.account
        self.store.disconnect_account(before["id"])
        disconnected = self.store.get_account(before["id"])
        self.assertIsNone(self.store.upsert_gmail_account(
            {"emailAddress": before["email"]}, "stale-oauth-credentials", NOW,
            expected_generation=before["generation"],
        ))
        self.assertEqual(self.store.get_account(before["id"]), disconnected)
        self.assertIsNone(self.store.get_account_tokens(before["id"]))
        # An explicit fresh reconnect is allowed from the disconnected state;
        # only one of several callbacks sharing its generation can commit.
        with ThreadPoolExecutor(max_workers=5) as workers:
            results = list(workers.map(lambda number: self.store.upsert_gmail_account(
                {"emailAddress": before["email"]}, f"encrypted-winner-{number}", NOW,
                expected_generation=disconnected["generation"],
            ), range(5)))
        winners = [account for account in results if account is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.get_account(before["id"]), winners[0])
        self.assertTrue(winners[0]["connected"])
        self.assertEqual(winners[0]["initial_since"], before["initial_since"])
        self.assertEqual(winners[0]["initial_before"], before["initial_before"])

    def test_oauth_absent_generation_sentinel_prevents_parallel_new_account_replacement(self):
        profile = {"emailAddress": "new-account@example.com", "historyId": "start-anchor"}
        first = self.store.upsert_gmail_account(profile, "encrypted-first", NOW, expected_generation="")
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.upsert_gmail_account(
            profile, "encrypted-stale-new-callback", NOW + timedelta(minutes=5), expected_generation="",
        ))
        self.assertEqual(self.store.get_account_tokens(first["id"]), "encrypted-first")
        self.assertEqual(self.store.get_account(first["id"]), first)


if __name__ == "__main__":
    unittest.main()
