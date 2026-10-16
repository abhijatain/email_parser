"""Persistent inbox behavior, ingestion, filtering, and simulated sync."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from email_parser.store import InboxStore
from email_parser.triage import CATEGORIES, PRIORITIES


def raw_email(subject="Imported project note", body="Please review the project proposal.", *, date="Fri, 16 Oct 2026 10:00:00 +0000", html=False, attachment=False):
    message = EmailMessage()
    message["From"] = "Jamie Reader <jamie@example.com>"
    message["To"] = "Alex Morgan <alex.morgan@example.com>"
    message["Subject"] = subject
    message["Date"] = date
    message.set_content(body, subtype="html" if html else "plain")
    if attachment:
        message.add_attachment(b"quarter,amount\nQ1,300\n", maintype="text", subtype="csv", filename="report.csv")
    return message.as_bytes()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "nested" / "inbox.sqlite3"
        self.store = InboxStore(self.path)

    def test_seed_is_realistic_valid_and_inserted_only_once(self):
        messages = self.store.list_messages()
        self.assertGreaterEqual(len(messages), 24)
        self.assertEqual({item["category"] for item in messages}, {row["id"] for row in CATEGORIES})
        self.assertGreater(len({item["priority"] for item in messages}), 1)
        self.assertEqual(len({item["id"] for item in messages}), len(messages))
        required = {"id", "subject", "sender", "to", "received_at", "text", "preview", "category", "priority", "score", "labels", "reasons", "unread", "starred", "archived", "attachments", "source", "overridden"}
        for item in messages:
            self.assertTrue(required.issubset(item))
            self.assertTrue(item["sender"]["address"].endswith("@example.com"))
            self.assertTrue(all(recipient["address"].endswith("@example.com") for recipient in item["to"]))
            self.assertEqual(item["source"], "demo")
            self.assertIn(item["priority"], PRIORITIES)
            datetime.fromisoformat(item["received_at"])
        again = InboxStore(self.path)
        self.assertEqual(messages, again.list_messages())

    def test_flags_and_manual_overrides_persist(self):
        message = self.store.list_messages()[0]
        flagged = self.store.update_message(message["id"], {"unread": False, "starred": True})
        self.assertFalse(flagged["unread"])
        self.assertTrue(flagged["starred"])
        self.assertFalse(flagged["overridden"])
        overridden = self.store.update_message(message["id"], {"category": "personal", "priority": "low"})
        self.assertEqual(overridden["category"], "personal")
        self.assertEqual(overridden["priority"], "low")
        self.assertGreaterEqual(overridden["score"], 0)
        self.assertLessEqual(overridden["score"], 34)
        self.assertTrue(overridden["overridden"])
        self.assertTrue(overridden["labels"])
        self.assertTrue(overridden["reasons"])
        self.assertEqual(InboxStore(self.path).get_message(message["id"]), overridden)

    def test_each_manual_priority_respects_its_score_band(self):
        message_id = self.store.list_messages()[0]["id"]
        for priority, lower, upper in [("critical", 85, 100), ("high", 65, 84), ("normal", 35, 64), ("low", 0, 34)]:
            with self.subTest(priority=priority):
                result = self.store.update_message(message_id, {"priority": priority})
                self.assertEqual(result["priority"], priority)
                self.assertGreaterEqual(result["score"], lower)
                self.assertLessEqual(result["score"], upper)

    def test_invalid_updates_are_rejected_without_partial_changes(self):
        message = self.store.list_messages()[0]
        for changes in [{"subject": "overwrite"}, {"unread": "false"}, {"starred": 1}, {"archived": None}, {"category": "fake"}, {"priority": "urgent"}, {"category": []}, {"priority": {}}, {"category": None}, {"priority": True}, {"unread": False, "priority": "fake"}]:
            with self.subTest(changes=changes):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.update_message(message["id"], changes)
                self.assertEqual(self.store.get_message(message["id"]), message)
        self.assertIsNone(self.store.get_message("missing-message"))
        self.assertIsNone(self.store.update_message("missing-message", {"unread": False}))

    def test_filters_search_and_views_intersect(self):
        item = self.store.import_email(raw_email(subject="Unique cobalt plan", body="Body includes a sapphire token."))
        item = self.store.update_message(item["id"], {"category": "work", "priority": "high", "unread": True, "starred": True})
        for term in ["COBALT", "SaPpHiRe", "JAMIE@EXAMPLE.COM", "Jamie Reader"]:
            with self.subTest(search=term):
                self.assertIn(item["id"], {row["id"] for row in self.store.list_messages(search=term)})
        self.assertEqual([row["id"] for row in self.store.list_messages(search="cobalt", category="work", priority="high", view="starred")], [item["id"]])
        self.assertEqual(self.store.list_messages(search="cobalt", category="finance"), [])
        self.assertEqual(self.store.list_messages(search="cobalt", priority="low"), [])
        self.store.update_message(item["id"], {"unread": False})
        self.assertEqual(self.store.list_messages(search="cobalt", view="unread"), [])
        self.store.update_message(item["id"], {"archived": True})
        for view in ["inbox", "unread", "starred"]:
            self.assertEqual(self.store.list_messages(search="cobalt", view=view), [])
        self.assertEqual([row["id"] for row in self.store.list_messages(search="cobalt", view="archived")], [item["id"]])
        self.store.update_message(item["id"], {"archived": False})
        self.assertEqual(self.store.list_messages(search="cobalt")[0]["id"], item["id"])

    def test_summary_counts_active_messages_and_categories(self):
        first = self.store.list_messages()[0]
        self.store.update_message(first["id"], {"archived": True, "starred": True, "unread": True})
        active = self.store.list_messages()
        archived = self.store.list_messages(view="archived")
        summary = self.store.summary()
        self.assertEqual(summary["total"], len(active))
        self.assertEqual(summary["unread"], sum(row["unread"] for row in active))
        self.assertEqual(summary["starred"], sum(row["starred"] for row in active))
        self.assertEqual(summary["important"], sum(row["priority"] in {"critical", "high"} for row in active))
        self.assertEqual(summary["archived"], len(archived))
        self.assertEqual(summary["demo_batches_remaining"], 3)
        for category in summary["categories"]:
            matching = [row for row in active if row["category"] == category["id"]]
            self.assertEqual(category["count"], len(matching))
            self.assertEqual(category["unread"], sum(row["unread"] for row in matching))
            self.assertEqual(category["important"], sum(row["priority"] in {"critical", "high"} for row in matching))

    def test_sync_is_finite_persistent_and_never_duplicates(self):
        initial = len(self.store.list_messages())
        added = 0
        for remaining in [2, 1, 0]:
            result = self.store.sync_demo()
            self.assertGreater(result["added"], 0)
            self.assertEqual(result["remaining_batches"], remaining)
            added += result["added"]
            self.store = InboxStore(self.path)
            self.assertEqual(self.store.summary()["demo_batches_remaining"], remaining)
        messages = self.store.list_messages()
        self.assertEqual(len(messages), initial + added)
        self.assertEqual(len({row["id"] for row in messages}), len(messages))
        self.assertEqual(self.store.sync_demo(), {"added": 0, "remaining_batches": 0})
        self.assertEqual(self.store.list_messages(), messages)

    def test_reset_restores_demo_but_preserves_imports(self):
        original_messages = self.store.list_messages()
        original_subjects = {row["subject"] for row in original_messages}
        imported = self.store.import_email(raw_email("Keep this import"))
        imported = self.store.update_message(imported["id"], {"category": "personal", "starred": True, "archived": True})
        self.store.update_message(original_messages[0]["id"], {"archived": True})
        self.store.sync_demo()
        self.store.reset_demo()
        active_demo = [row for row in self.store.list_messages() if row["source"] == "demo"]
        self.assertEqual({row["subject"] for row in active_demo}, original_subjects)
        self.assertEqual(len(active_demo), len(original_messages))
        self.assertEqual(self.store.get_message(imported["id"]), imported)
        self.assertEqual(self.store.summary()["demo_batches_remaining"], 3)

    def test_import_reuses_parser_is_idempotent_and_preserves_attachment_metadata(self):
        data = raw_email("Café project report ✓", attachment=True)
        before = self.store.summary()["total"]
        imported = self.store.import_email(data)
        self.assertEqual(imported["source"], "import")
        self.assertEqual(imported["subject"], "Café project report ✓")
        self.assertEqual(imported["sender"], {"name": "Jamie Reader", "address": "jamie@example.com"})
        self.assertEqual(imported["category"], "work")
        self.assertEqual(imported["attachments"][0]["filename"], "report.csv")
        self.assertEqual(imported["attachments"][0]["content_type"], "text/csv")
        self.assertEqual(imported["attachments"][0]["size"], len(b"quarter,amount\nQ1,300\n"))
        self.assertNotIn("data", imported["attachments"][0])
        self.assertEqual(self.store.import_email(data)["id"], imported["id"])
        self.assertEqual(self.store.summary()["total"], before + 1)
        self.assertEqual(InboxStore(self.path).import_email(data)["id"], imported["id"])

    def test_html_only_import_is_plain_text(self):
        item = self.store.import_email(raw_email("HTML import", "<html><body><p>Hello <b>Alex</b> &amp; friends.</p><script>alert('bad')</script></body></html>", html=True))
        self.assertIn("Hello", item["text"])
        self.assertIn("Alex", item["text"])
        self.assertIn("& friends", item["text"])
        self.assertNotIn("<p>", item["text"])
        self.assertNotIn("<script>", item["text"])

    def test_ranking_prioritizes_score_then_date_and_is_stable(self):
        ids = []
        for subject, date in [("Rank older", "Thu, 15 Oct 2026 10:00:00 +0000"), ("Rank newer", "Fri, 16 Oct 2026 10:00:00 +0000"), ("Rank tied", "Fri, 16 Oct 2026 10:00:00 +0000")]:
            item = self.store.import_email(raw_email(subject, date=date))
            self.store.update_message(item["id"], {"priority": "normal"})
            ids.append(item["id"])
        ranked = self.store.list_messages(search="Rank ")
        self.assertEqual(ranked[-1]["id"], ids[0])
        self.assertEqual(ranked, InboxStore(self.path).list_messages(search="Rank "))
        self.store.update_message(ids[0], {"priority": "critical"})
        self.assertEqual(self.store.list_messages(search="Rank ")[0]["id"], ids[0])

    def test_concurrent_ingestion_and_sync_remain_atomic(self):
        data = raw_email("Concurrent import")
        with ThreadPoolExecutor(max_workers=6) as workers:
            imported = list(workers.map(lambda _: self.store.import_email(data), range(6)))
        self.assertEqual(len({row["id"] for row in imported}), 1)
        before = self.store.summary()["total"]
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(workers.map(lambda _: self.store.sync_demo(), range(6)))
        self.assertEqual(sum(result["added"] > 0 for result in results), 3)
        self.assertEqual(self.store.summary()["demo_batches_remaining"], 0)
        self.assertEqual(self.store.summary()["total"], before + sum(result["added"] for result in results))


if __name__ == "__main__":
    unittest.main()
