"""Public parser behavior for real MIME email structures."""

import base64
import json
import tempfile
import unittest
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from email_parser import parse_email, parse_file


class ParserTests(unittest.TestCase):
    def test_decodes_unicode_headers_addresses_and_body(self):
        subject = base64.b64encode("Résumé ✓".encode()).decode()
        sender = base64.b64encode("José".encode()).decode()
        raw = (
            f"Subject: =?UTF-8?B?{subject}?=\r\n"
            f"From: =?UTF-8?B?{sender}?= <jose@example.com>\r\n"
            "To: Alice <alice@example.com>, bob@example.com\r\n"
            "Cc: Team <team@example.com>\r\n"
            "Bcc: private@example.com\r\n"
            "Reply-To: Support <support@example.com>\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            "Caf=C3=A9\r\n"
        ).encode("ascii")

        parsed = parse_email(raw)

        self.assertEqual(parsed.subject, "Résumé ✓")
        self.assertEqual(parsed.from_[0].name, "José")
        self.assertEqual(parsed.from_[0].address, "jose@example.com")
        self.assertEqual([a.address for a in parsed.to], ["alice@example.com", "bob@example.com"])
        self.assertEqual(parsed.cc[0].address, "team@example.com")
        self.assertEqual(parsed.bcc[0].address, "private@example.com")
        self.assertEqual(parsed.reply_to[0].address, "support@example.com")
        self.assertEqual(parsed.text.strip(), "Café")

    def test_normalizes_date_and_message_id(self):
        parsed = parse_email(
            b"Date: Tue, 03 Jun 2025 12:34:56 +0530\r\n"
            b"Message-ID: <example-123@example.com>\r\n\r\nhello"
        )

        self.assertEqual(datetime.fromisoformat(parsed.date).isoformat(), "2025-06-03T12:34:56+05:30")
        self.assertEqual(parsed.message_id, "<example-123@example.com>")

    def test_preserves_duplicate_headers_in_order(self):
        parsed = parse_email(
            b"Received: by relay-one\r\n"
            b"X-Trace: first\r\n"
            b"Received: by relay-two\r\n"
            b"X-Trace: second\r\n\r\nhello"
        )

        headers = [(h["name"].lower(), h["value"]) for h in parsed.headers]
        self.assertEqual(headers, [
            ("received", "by relay-one"),
            ("x-trace", "first"),
            ("received", "by relay-two"),
            ("x-trace", "second"),
        ])

    def test_collects_alternative_bodies_and_binary_attachment(self):
        message = EmailMessage()
        message["Subject"] = "Two formats"
        message.set_content("Plain body π")
        message.add_alternative("<p>HTML body π</p>", subtype="html")
        payload = b"\x00\xff\x01attachment\r\n"
        message.add_attachment(payload, maintype="application", subtype="octet-stream", filename="report.bin")

        parsed = parse_email(message.as_bytes())

        self.assertEqual(parsed.text.strip(), "Plain body π")
        self.assertEqual(parsed.html.strip(), "<p>HTML body π</p>")
        self.assertEqual(len(parsed.attachments), 1)
        attachment = parsed.attachments[0]
        self.assertEqual(attachment.filename, "report.bin")
        self.assertEqual(attachment.content_type, "application/octet-stream")
        self.assertEqual(attachment.disposition, "attachment")
        self.assertEqual(attachment.size, len(payload))
        self.assertEqual(attachment.data, payload)

    def test_inline_image_has_content_id(self):
        message = EmailMessage()
        message.set_content('<img src="cid:logo@example.com">', subtype="html")
        message.add_related(
            b"\x89PNG\r\n\x1a\n", maintype="image", subtype="png",
            cid="<logo@example.com>", disposition="inline", filename="logo.png",
        )

        parsed = parse_email(message.as_bytes())

        self.assertIn("cid:logo@example.com", parsed.html)
        self.assertEqual(len(parsed.attachments), 1)
        attachment = parsed.attachments[0]
        self.assertEqual(attachment.content_id.strip("<>"), "logo@example.com")
        self.assertEqual(attachment.disposition, "inline")
        self.assertEqual(attachment.data, b"\x89PNG\r\n\x1a\n")

    def test_attached_message_is_not_merged_into_outer_body(self):
        inner = EmailMessage()
        inner["Subject"] = "Forwarded subject"
        inner.set_content("Private attached body")
        outer = EmailMessage()
        outer.set_content("Outer body")
        outer.add_attachment(inner, filename="forwarded.eml")

        parsed = parse_email(outer.as_bytes())

        self.assertEqual(parsed.text.strip(), "Outer body")
        self.assertEqual(len(parsed.attachments), 1)
        attachment = parsed.attachments[0]
        self.assertEqual(attachment.content_type, "message/rfc822")
        self.assertEqual(attachment.filename, "forwarded.eml")
        self.assertIn(b"Forwarded subject", attachment.data)
        self.assertIn(b"Private attached body", attachment.data)
        self.assertEqual(attachment.size, len(attachment.data))

    def test_unknown_charset_is_recoverable_and_reported(self):
        parsed = parse_email(
            b"Content-Type: text/plain; charset=not-a-real-charset\r\n\r\nhello caf\xc3\xa9"
        )

        self.assertEqual(parsed.text, "hello café")
        self.assertTrue(parsed.warnings)
        self.assertTrue(all(isinstance(item, str) for item in parsed.warnings))

    def test_text_attachment_is_not_merged_into_body(self):
        message = EmailMessage()
        message.set_content("Body text")
        message.add_attachment("Attached text", subtype="plain", filename="notes.txt")

        parsed = parse_email(message.as_bytes())

        self.assertEqual(parsed.text.strip(), "Body text")
        self.assertEqual(len(parsed.attachments), 1)
        self.assertEqual(parsed.attachments[0].filename, "notes.txt")
        self.assertEqual(parsed.attachments[0].data.strip(), b"Attached text")

    def test_unsuitable_known_charsets_recover_body_and_warn(self):
        for charset, payload, expected in [
            ("idna", b"\xff", "\ufffd"),
            ("undefined", b"foo", "foo"),
        ]:
            with self.subTest(charset=charset):
                raw = f"Content-Type: text/plain; charset={charset}\r\n\r\n".encode() + payload

                parsed = parse_email(raw)

                self.assertEqual(parsed.text, expected)
                self.assertTrue(parsed.warnings)
                json.dumps(parsed.to_dict())

    def test_malformed_multipart_is_recoverable_and_reported(self):
        parsed = parse_email(b"Content-Type: multipart/mixed; boundary=missing\r\n\r\nbody without boundaries")

        self.assertTrue(parsed.warnings)
        json.dumps(parsed.to_dict())

    def test_invalid_date_is_not_emitted_as_a_valid_timestamp(self):
        parsed = parse_email(b"Date: someday after lunch\r\n\r\nhello")
        self.assertIsNone(parsed.date)

    def test_file_api_and_json_exclude_attachment_bytes(self):
        message = EmailMessage()
        message["From"] = "Sender <sender@example.com>"
        message.set_content("hello")
        message.add_attachment(b"\x00\xff", maintype="application", subtype="octet-stream", filename="binary.bin")
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "mail.eml"
            source.write_bytes(message.as_bytes())
            parsed = parse_file(source)
            parsed_from_string = parse_file(str(source))

        result = parsed.to_dict()
        self.assertEqual(result, parsed_from_string.to_dict())
        self.assertEqual(result["from"], [{"name": "Sender", "address": "sender@example.com"}])
        self.assertNotIn("from_", result)
        self.assertEqual(result["text"].strip(), "hello")
        self.assertNotIn("data", result["attachments"][0])
        self.assertEqual(json.loads(json.dumps(result)), result)


if __name__ == "__main__":
    unittest.main()
