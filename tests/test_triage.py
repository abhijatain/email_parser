"""Explainable classification and priority behavior for the simulated inbox."""

import unittest
from datetime import datetime, timezone
from email.message import EmailMessage

from email_parser import parse_email
from email_parser.triage import CATEGORIES, PRIORITIES, classify_email


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
BANDS = {"critical": (85, 100), "high": (65, 84), "normal": (35, 64), "low": (0, 34)}


def classify(subject, body, sender="Morgan <morgan@example.com>", **headers):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "Alex Morgan <alex.morgan@example.com>"
    message["Subject"] = subject
    message["Date"] = "Fri, 16 Oct 2026 11:00:00 +0000"
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    message.set_content(body)
    return classify_email(parse_email(message.as_bytes()), now=NOW)


class TriageTests(unittest.TestCase):
    def test_public_taxonomy_and_priority_bands(self):
        self.assertEqual({row["id"] for row in CATEGORIES}, {
            "work", "finance", "personal", "travel", "shopping", "newsletters", "security", "other",
        })
        self.assertEqual(PRIORITIES, ["critical", "high", "normal", "low"])
        for category in CATEGORIES:
            self.assertTrue(category["name"])
            self.assertTrue(category["description"])

    def test_representative_mail_categories(self):
        examples = [
            ("work", "Project launch: approval required", "Please review the project proposal for our team before today's deadline.", "Team lead <team@example.com>"),
            ("finance", "Bank statement and invoice payment", "Your bank account statement is ready. Invoice payment is due next week.", "Bank <bank@example.com>"),
            ("personal", "Family dinner this weekend", "Hi Alex, are you coming to dinner with the family on Sunday? Love, Mom.", "Mom <mom@example.com>"),
            ("travel", "Flight booking confirmation", "Your flight itinerary and boarding pass for your trip are ready.", "Airline <travel@example.com>"),
            ("shopping", "Your order has shipped", "Order confirmation: your package is on its way. Track your delivery here.", "Store <orders@example.com>"),
            ("newsletters", "Weekly newsletter digest", "This week's stories and reading list. Unsubscribe from this newsletter.", "Weekly digest <newsletter@example.com>"),
            ("security", "Security alert: suspicious sign-in", "An unauthorized login from a new device was detected. Reset your password immediately.", "Account security <security@example.com>"),
            ("other", "A small note", "Here is the blue pebble we mentioned.", "Robin <robin@example.com>"),
        ]
        for expected, subject, body, sender in examples:
            with self.subTest(category=expected):
                result = classify(subject, body, sender)
                self.assertEqual(result["category"], expected)
                self.assertIn(result["priority"], BANDS)
                self.assertIsInstance(result["score"], int)
                lower, upper = BANDS[result["priority"]]
                self.assertGreaterEqual(result["score"], lower)
                self.assertLessEqual(result["score"], upper)
                self.assertTrue(result["reasons"])
                self.assertTrue(all(isinstance(reason, str) and reason for reason in result["reasons"]))
                self.assertTrue(all(isinstance(label, str) for label in result["labels"]))

    def test_security_and_action_deadlines_rank_above_newsletters(self):
        security = classify("Security alert: unauthorized login", "Suspicious sign-in detected. Reset your password immediately.", "Security <security@example.com>")
        deadline = classify("Action required: approve project by today", "Please approve this urgent project launch before today's deadline. Your response is required.", "Manager <team@example.com>")
        newsletter = classify("Weekly newsletter", "A digest of interesting stories. Unsubscribe anytime.", "Digest <newsletter@example.com>")
        for important in (security, deadline):
            self.assertIn(important["priority"], {"critical", "high"})
            self.assertGreater(important["score"], newsletter["score"])
            self.assertTrue(important["reasons"])
        self.assertEqual(newsletter["priority"], "low")

    def test_promotional_false_urgency_does_not_become_important(self):
        promotion = classify(
            "URGENT: last chance sale ends today! Act now!",
            "Limited-time offer: 60% off. Shop now before midnight. Buy today! Unsubscribe from promotional offers.",
            "Store offers <offers@example.com>",
            List_Unsubscribe="<mailto:unsubscribe@example.com>",
        )
        self.assertEqual(promotion["priority"], "low")
        self.assertLessEqual(promotion["score"], 34)

    def test_routine_delivery_explicitly_saying_no_action_is_low_priority(self):
        delivery = classify(
            "Order update",
            "Your package has shipped. No action required. This is not urgent.",
            "Orders <orders@example.com>",
        )
        self.assertEqual(delivery["category"], "shopping")
        self.assertEqual(delivery["priority"], "low")
        self.assertLessEqual(delivery["score"], 34)

    def test_operational_mailing_list_keeps_production_incident_critical(self):
        incident = classify(
            "URGENT: production outage",
            "Service outage affecting customers. Please review the incident immediately today.",
            List_ID="<production-alerts.example.com>",
        )
        self.assertEqual(incident["category"], "work")
        self.assertEqual(incident["priority"], "critical")
        self.assertGreaterEqual(incident["score"], 85)

    def test_client_discount_approval_is_an_important_work_decision(self):
        approval = classify(
            "Client approval needed today",
            "Please approve the 10% discount requested by the client. This decision is blocking the project.",
        )
        self.assertEqual(approval["category"], "work")
        self.assertIn(approval["priority"], {"high", "critical"})
        self.assertGreaterEqual(approval["score"], 65)

    def test_promotional_footer_does_not_hide_password_change_alert(self):
        alert = classify(
            "Your password has been changed",
            "If this was not you, review your account immediately.\n\nMembership benefits include discount offers.",
        )
        self.assertEqual(alert["category"], "security")
        self.assertEqual(alert["priority"], "critical")
        self.assertGreaterEqual(alert["score"], 85)

    def test_classification_is_deterministic_and_case_insensitive(self):
        first = classify("Flight itinerary", "Your flight booking and boarding pass are ready.", "Airline <travel@example.com>")
        self.assertEqual(first, classify("Flight itinerary", "Your flight booking and boarding pass are ready.", "Airline <travel@example.com>"))
        upper = classify("FLIGHT ITINERARY", "YOUR FLIGHT BOOKING AND BOARDING PASS ARE READY.", "AIRLINE <TRAVEL@EXAMPLE.COM>")
        self.assertEqual(first["category"], upper["category"])
        self.assertEqual(first["priority"], upper["priority"])
        self.assertEqual(first["score"], upper["score"])

    def test_empty_email_has_valid_explainable_fallback(self):
        result = classify_email(parse_email(b""), now=NOW)
        self.assertEqual(result["category"], "other")
        self.assertIn(result["priority"], BANDS)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)
        self.assertTrue(result["reasons"])


if __name__ == "__main__":
    unittest.main()
