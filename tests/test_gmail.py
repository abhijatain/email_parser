"""Gmail REST protocol and safe failures with no live mailbox access."""

import base64
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from email_parser.gmail import (
    API_BASE, GmailAuthError, GmailClient, GmailError, GmailHistoryExpired,
    GmailMessageMissing, https_request,
)


class Transport:
    def __init__(self, result=None, status=200):
        self.result = {} if result is None else result
        self.status = status
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.status, {}, json.dumps(self.result).encode()


class GmailTests(unittest.TestCase):
    def test_profile_uses_fixed_me_endpoint_and_bearer_header(self):
        transport = Transport({"emailAddress": "alex@example.com", "historyId": "123"})
        profile = GmailClient("test-access", request=transport).profile()
        self.assertEqual(profile["emailAddress"], "alex@example.com")
        method, url, kwargs = transport.calls[0]
        self.assertEqual((method, url), ("GET", API_BASE + "/profile"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-access")
        self.assertIsNone(kwargs["body"])

    def test_epoch_query_covers_exact_window_edges_and_pagination(self):
        transport = Transport({"messages": [{"id": "abc123"}], "nextPageToken": "page-2+/="})
        start = datetime(2026, 10, 14, 0, 0, 0, 500_000, timezone.utc)
        end = datetime(2026, 10, 17, 0, 0, 0, 500_000, timezone.utc)
        result = GmailClient("access", request=transport).list_messages(start, end, "previous+/=")
        query = parse_qs(urlsplit(transport.calls[0][1]).query)
        self.assertEqual(query["q"], [f"after:{int(start.timestamp()) - 1} before:{int(end.timestamp()) + 1}"])
        self.assertEqual(query["includeSpamTrash"], ["false"])
        self.assertEqual(query["pageToken"], ["previous+/="])
        self.assertEqual(result["nextPageToken"], "page-2+/=")

    def test_empty_listing_and_unbounded_upper_window(self):
        transport = Transport({})
        result = GmailClient("access", request=transport).list_messages(datetime.now(timezone.utc))
        self.assertEqual(result["messages"], [])
        self.assertNotIn("before:", transport.calls[0][1])

    def test_raw_email_decoding_preserves_bytes_and_uses_internal_date(self):
        raw = b"From: sender@example.com\r\nDate: Mon, 1 Jan 2001 00:00:00 +0000\r\n\r\nHello \xff"
        value = {"id": "abc123", "raw": base64.urlsafe_b64encode(raw).decode().rstrip("="),
                 "internalDate": "1792195200123", "threadId": "thread123", "labelIds": ["INBOX", "UNREAD"]}
        transport = Transport(value)
        message = GmailClient("access", request=transport).get_message("abc123")
        self.assertEqual(message["raw"], raw)
        self.assertEqual(message["internal_date"], datetime.fromtimestamp(1792195200.123, timezone.utc))
        self.assertEqual(message["labels"], ["INBOX", "UNREAD"])
        self.assertEqual(parse_qs(urlsplit(transport.calls[0][1]).query), {"format": ["raw"]})

    def test_malformed_raw_payloads_are_rejected_strictly(self):
        base = {"id": "a", "internalDate": "1000", "threadId": "t", "raw": "SGVsbG8"}
        for raw in ("SGVsbG8!", "SGVsbG8\n", "a", "SGVsbG9", "++//", "YQ=", "YQ===", "é", 123):
            with self.subTest(raw=raw), self.assertRaises(GmailError):
                GmailClient("access", request=Transport({**base, "raw": raw})).get_message("a")

    def test_bad_remote_metadata_rejected(self):
        base = {"id": "a", "internalDate": "1000", "threadId": "t", "raw": "YQ"}
        for field, value in (("id", "other"), ("internalDate", "NaN"), ("internalDate", "9999999999999999"),
                             ("labelIds", "INBOX"), ("labelIds", [None]), ("threadId", "../../evil")):
            with self.subTest(field=field, value=value), self.assertRaises(GmailError):
                GmailClient("access", request=Transport({**base, field: value})).get_message("a")

    def test_message_size_is_bounded(self):
        value = {"id": "a", "internalDate": "1000", "threadId": "t",
                 "raw": base64.urlsafe_b64encode(b"123456789").decode()}
        with patch("email_parser.gmail.MAX_MESSAGE_BYTES", 4), self.assertRaises(GmailError):
            GmailClient("access", request=Transport(value)).get_message("a")

    def test_history_preserves_all_change_arrays(self):
        value = {"historyId": "987", "nextPageToken": "next", "history": [{"id": "986",
                 "messagesAdded": [{"message": {"id": "a"}}], "labelsRemoved": [{"message": {"id": "b"}}]}]}
        transport = Transport(value)
        self.assertEqual(GmailClient("access", request=transport).history("123", "previous"), value)
        query = parse_qs(urlsplit(transport.calls[0][1]).query)
        self.assertEqual(query["startHistoryId"], ["123"])
        self.assertEqual(query["pageToken"], ["previous"])
        self.assertNotIn("historyTypes", query)

    def test_safe_status_errors_never_include_private_provider_body(self):
        for status, exception in ((401, GmailAuthError), (403, GmailAuthError), (429, GmailError),
                                   (500, GmailError), (302, GmailError), (400, GmailError)):
            with self.subTest(status=status), self.assertRaises(exception) as caught:
                GmailClient("secret-access", request=Transport({"error": "PRIVATE_BODY_secret-access"}, status)).profile()
            self.assertNotIn("PRIVATE_BODY", str(caught.exception))
            self.assertNotIn("secret-access", str(caught.exception))
            self.assertEqual(caught.exception.status, status)

    def test_expired_history_and_deleted_message_are_distinct(self):
        client = GmailClient("access", request=Transport({}, 404))
        with self.assertRaises(GmailHistoryExpired):
            client.history("1")
        with self.assertRaises(GmailMessageMissing) as caught:
            client.get_message("a")
        self.assertEqual(caught.exception.status, 404)

    def test_invalid_json_and_transport_errors_are_sanitized(self):
        def transport(*args, **kwargs):
            raise RuntimeError("PRIVATE_URL_secret-access")
        with self.assertRaises(GmailError) as caught:
            GmailClient("secret-access", request=transport).profile()
        self.assertNotIn("PRIVATE", str(caught.exception))
        for data in (b"not-json", b"[]", b"null", b"\xff"):
            with self.subTest(data=data), self.assertRaises(GmailError):
                GmailClient("access", request=lambda *args, **kwargs: (200, {}, data)).profile()

    def test_invalid_profile_id_dates_and_tokens_rejected_before_request(self):
        for token in ("", "access\r\nInjected: yes", None):
            with self.subTest(token=token), self.assertRaises(GmailAuthError):
                GmailClient(token)
        transport = Transport()
        client = GmailClient("access", request=transport)
        with self.assertRaises(GmailError):
            client.get_message("../../evil")
        with self.assertRaises(GmailError):
            client.list_messages(datetime.now())
        self.assertEqual(transport.calls, [])
        for value in ({"emailAddress": "invalid", "historyId": "1"}, {"emailAddress": "a@b", "historyId": None}):
            with self.assertRaises(GmailError):
                GmailClient("access", request=Transport(value)).profile()

    def test_default_transport_rejects_nonfixed_or_insecure_hosts(self):
        for url in ("http://gmail.googleapis.com/", "https://evil.example/", "https://gmail.googleapis.com.evil.example/",
                    "https://token@oauth2.googleapis.com/token", "https://gmail.googleapis.com:444/"):
            with self.subTest(url=url), self.assertRaises(GmailError):
                https_request("GET", url, headers={}, body=None, timeout=1, max_bytes=100)


if __name__ == "__main__":
    unittest.main()
