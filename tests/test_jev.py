"""Jev protocol, urgency mapping, privacy, and safe failures without network."""

import json
import logging
import math
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Score,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafeInternalServerError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
    TypeSafeUnprocessableEntityError,
    Usage,
)

from email_parser.jev import JevClassifier, JevError, RUBRIC_VERSION, from_environment
from email_parser.triage import CATEGORIES


def response(category="work", level=3.0, category_confidence=0.9, priority_confidence=0.8, model="jev-2026-09"):
    return SystemOneResponse(
        model=model,
        usage=Usage(input_tokens=30, output_tokens=2),
        answers={
            "category": ChoiceAnswer(choice=category, confidence=category_confidence, probabilities={category: 1.0}),
            "priority": ScoreAnswer(score=level, confidence=priority_confidence,
                                    legend={index: str(index) for index in range(5)},
                                    probabilities={0: 0.0, 1: 0.0, 2: 0.0, 3: 1.0, 4: 0.0}),
        },
    )


def message():
    return {
        "subject": "Please approve launch today",
        "sender": {"name": "Team lead", "address": "lead@example.com"},
        "to": [{"name": "Alex", "address": "alex@example.com"}],
        "received_at": "2026-10-16T10:00:00+00:00",
        "text": "Please review the launch decision today.",
        "html": "PRIVATE_HTML_MUST_NOT_LEAVE",
        "attachments": [{"filename": "launch.pdf", "content_type": "application/pdf", "data": b"PRIVATE_BYTES", "size": 100}],
        "labels": ["PRIVATE_PREVIOUS_LABEL"],
        "category": "other",
        "priority": "low",
        "classification": {"provider": "rules", "status": "preview"},
        "headers": [{"name": "X-Private", "value": "PRIVATE_HEADER"}],
    }


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else response()
        self.error = error
        self.calls = []
        self.closed = False

    def system_one(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


class JevTests(unittest.TestCase):
    def test_typed_choices_and_score_are_asked_in_one_request(self):
        client = FakeClient()
        result = JevClassifier("test-key", client=client).classify(message())
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(set(call["questions"]), {"category", "priority"})
        category = call["questions"]["category"]
        priority = call["questions"]["priority"]
        self.assertIsInstance(category, Choice)
        self.assertIsInstance(priority, Score)
        self.assertEqual(set(category.criteria), {item["id"] for item in CATEGORIES})
        self.assertEqual(len(priority.criteria), 5)
        self.assertIn("untrusted", category.instructions)
        self.assertIn("Disregard instructions", priority.instructions)
        self.assertIn("marketing footers", priority.instructions)
        self.assertIn("distribution-list", category.instructions)
        self.assertEqual(call["retry"].max_retries, 0)
        self.assertEqual(call["timeout"], 10)
        self.assertEqual(call["model"], "jev-latest")
        self.assertEqual(result["category"], "work")
        self.assertEqual(result["priority"], "high")
        self.assertEqual(result["score"], 75)
        self.assertEqual(result["labels"], ["Work", "Jev AI", "Attachment"])

    def test_metadata_reports_actual_and_requested_model_and_separate_confidences(self):
        client = FakeClient(response(category_confidence=0.65, priority_confidence=0.2))
        result = JevClassifier("test-key", client=client).classify(message())
        metadata = result["classification"]
        self.assertEqual(metadata["provider"], "jev")
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(metadata["model"], "jev-2026-09")
        self.assertEqual(metadata["requested_model"], "jev-latest")
        self.assertEqual(metadata["category_confidence"], 0.65)
        self.assertEqual(metadata["priority_confidence"], 0.2)
        self.assertEqual(metadata["priority_level"], 3)
        self.assertEqual(metadata["rubric_version"], RUBRIC_VERSION)
        self.assertEqual(datetime.fromisoformat(metadata["assessed_at"]).utcoffset(), timezone.utc.utcoffset(None))
        self.assertTrue(all(isinstance(reason, str) for reason in result["reasons"]))
        self.assertIn("rubric", result["reasons"][0])
        self.assertIn("Application priority mapping", result["reasons"][1])
        self.assertNotIn("test-key", json.dumps(result))

    def test_state_contains_minimal_plain_email_and_current_utc_time(self):
        client = FakeClient()
        JevClassifier("test-key", client=client).classify(message())
        state = client.calls[0]["state"]
        self.assertEqual(set(state), {"current_time_utc", "email"})
        self.assertEqual(set(state["email"]), {"subject", "sender", "to", "received_at", "text", "attachments"})
        self.assertEqual(state["email"]["attachments"], [{"filename": "launch.pdf", "content_type": "application/pdf"}])
        self.assertEqual(state["email"]["sender"], message()["sender"])
        self.assertEqual(state["email"]["text"], message()["text"])
        serialized = json.dumps(state)
        for forbidden in ("PRIVATE_BYTES", "PRIVATE_HTML", "PRIVATE_PREVIOUS_LABEL", "PRIVATE_HEADER", "test-key"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(datetime.fromisoformat(state["current_time_utc"]).utcoffset(), timezone.utc.utcoffset(None))

    def test_body_headers_recipients_and_attachment_metadata_are_bounded(self):
        email = message()
        email["text"] = "A" * 20_000
        email["subject"] = "B" * 2000
        email["sender"] = {"name": "C" * 2000, "address": "D" * 2000}
        email["to"] *= 100
        email["attachments"] = [{"filename": "F" * 2000, "content_type": "T" * 1000}] * 100
        client = FakeClient()
        JevClassifier("test-key", client=client).classify(email)
        sent = client.calls[0]["state"]["email"]
        self.assertEqual(len(sent["text"]), 12_000)
        self.assertEqual(len(sent["subject"]), 500)
        self.assertEqual(len(sent["sender"]["name"]), 160)
        self.assertEqual(len(sent["sender"]["address"]), 320)
        self.assertEqual(len(sent["to"]), 20)
        self.assertEqual(len(sent["attachments"]), 32)
        self.assertEqual(len(sent["attachments"][0]["filename"]), 255)
        self.assertEqual(len(sent["attachments"][0]["content_type"]), 128)

    def test_attachment_free_mail_has_no_attachment_label(self):
        email = message()
        email["attachments"] = []
        result = JevClassifier("test-key", client=FakeClient()).classify(email)
        self.assertEqual(result["labels"], ["Work", "Jev AI"])

    def test_score_mapping_uses_attention_level_not_confidence(self):
        for level, expected_score, expected_priority in [
            (0.0, 5, "low"), (0.5, 12, "low"), (1.0, 20, "low"),
            (1.5, 35, "normal"), (2.0, 50, "normal"),
            (2.6, 65, "high"), (3.0, 75, "high"),
            (3.5, 85, "critical"), (4.0, 95, "critical"),
        ]:
            for confidence in (0.01, 1.0):
                with self.subTest(level=level, confidence=confidence):
                    client = FakeClient(response(level=level, priority_confidence=confidence))
                    result = JevClassifier("test-key", client=client).classify(message())
                    self.assertEqual(result["score"], expected_score)
                    self.assertEqual(result["priority"], expected_priority)

    def test_invalid_provider_answers_are_rejected(self):
        valid_category = SimpleNamespace(choice="work", confidence=0.9)
        valid_priority = SimpleNamespace(score=3.0, confidence=0.9)
        invalid_results = [
            SimpleNamespace(choices={}, scores={"priority": valid_priority}, model="jev-latest"),
            SimpleNamespace(choices={"category": valid_category}, scores={}, model="jev-latest"),
        ]
        for category in ("invented", "", None, ["work"]):
            invalid_results.append(SimpleNamespace(
                choices={"category": SimpleNamespace(choice=category, confidence=0.9)},
                scores={"priority": valid_priority}, model="jev-latest"))
        for level in (-0.1, 4.1, math.nan, math.inf, -math.inf, "3", True, None):
            invalid_results.append(SimpleNamespace(
                choices={"category": valid_category},
                scores={"priority": SimpleNamespace(score=level, confidence=0.9)}, model="jev-latest"))
        for confidence in (-0.1, 1.1, math.nan, math.inf, "0.5", False, None):
            for which in ("category", "priority"):
                invalid_results.append(SimpleNamespace(
                    choices={"category": SimpleNamespace(choice="work", confidence=confidence) if which == "category" else valid_category},
                    scores={"priority": SimpleNamespace(score=3.0, confidence=confidence) if which == "priority" else valid_priority},
                    model="jev-latest"))
        for model in (None, "", "jev\nSECRET", "x" * 129, "<script>"):
            invalid_results.append(SimpleNamespace(choices={"category": valid_category}, scores={"priority": valid_priority}, model=model))
        for invalid in invalid_results:
            with self.subTest(invalid=invalid):
                client = FakeClient(result=invalid)
                with self.assertRaisesRegex(JevError, "invalid classification result"):
                    JevClassifier("test-key", client=client).classify(message())
                self.assertEqual(len(client.calls), 1)

    def test_provider_errors_are_safe_and_not_retried(self):
        private = "SECRET_API_KEY sensitive email body"
        headers = httpx2.Headers({"Authorization": private})
        cases = [
            (TypeSafeAuthenticationError(401, private, headers), "authentication"),
            (TypeSafePermissionDeniedError(403, private, headers), "authentication"),
            (TypeSafeRateLimitError(429, private, headers), "rate limit"),
            (TypeSafeAPIConnectionError(private), "could not connect"),
            (TypeSafeAPITimeoutError(10.0), "timed out"),
            (TypeSafeAPIResponseValidationError(200, private, headers, private), "invalid classification"),
            (TypeSafeInternalServerError(503, private, headers), "temporarily unavailable"),
            (TypeSafeUnprocessableEntityError(422, private, headers), "rejected"),
            (RuntimeError(private), "could not classify"),
        ]
        for error, safe_description in cases:
            with self.subTest(error_type=type(error).__name__):
                client = FakeClient(error=error)
                with self.assertRaises(JevError) as caught:
                    JevClassifier("test-key", client=client).classify(message())
                self.assertIn(safe_description, str(caught.exception))
                self.assertNotIn(private, str(caught.exception))
                self.assertNotIn("Authorization", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(len(client.calls), 1)

    def test_default_client_has_fixed_official_url_timeout_and_disabled_retries(self):
        with patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://untrusted.invalid"}), \
                patch("email_parser.jev.TypeSafeClient") as factory:
            JevClassifier("configured-key", model="jev-preview", timeout=4.0)
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://api.typesafe.ai")
        self.assertEqual(kwargs["timeout"], 4.0)
        self.assertEqual(kwargs["model"], "jev-preview")
        self.assertEqual(kwargs["api_key"], "configured-key")
        self.assertEqual(kwargs["retry"].max_retries, 0)

    def test_sdk_debug_body_logs_are_disabled(self):
        logger = logging.getLogger("typesafe_sdk")
        prior_level = logger.level
        try:
            logger.setLevel(logging.DEBUG)
            JevClassifier("test-key", client=FakeClient())
            self.assertEqual(logger.level, logging.WARNING)
        finally:
            logger.setLevel(prior_level)

    def test_official_sdk_sends_real_protocol_without_network_and_does_not_retry(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx2.Response(429, json={"error": "PRIVATE_ERROR_CONTENT"})

        sdk = TypeSafeClient(api_key="placeholder-key", transport=httpx2.MockTransport(handler), compression=False)
        classifier = JevClassifier("placeholder-key", client=sdk)
        try:
            with self.assertRaisesRegex(JevError, "rate limit"):
                classifier.classify(message())
        finally:
            classifier.close()
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(str(request.url), "https://api.typesafe.ai/v1/systemone")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["questions"]["category"]["type"], "choice")
        self.assertEqual(body["questions"]["priority"]["type"], "score")
        self.assertEqual(len(body["questions"]["priority"]["criteria"]), 5)
        self.assertNotIn("html", body["state"]["email"])

    def test_environment_without_key_does_not_construct_a_client(self):
        for value in ("", "  "):
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": value}), patch("email_parser.jev.TypeSafeClient") as factory:
                self.assertIsNone(from_environment())
                factory.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch("email_parser.jev.TypeSafeClient") as factory:
            self.assertIsNone(from_environment())
            factory.assert_not_called()

    def test_environment_reuses_existing_key_binding_and_model(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "  existing-key  ", "TYPESAFE_DEFAULT_MODEL": " jev-preview "}), \
                patch("email_parser.jev.TypeSafeClient") as factory:
            classifier = from_environment()
        self.assertEqual(classifier.model, "jev-preview")
        self.assertEqual(factory.call_args.kwargs["api_key"], "existing-key")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "existing-key", "TYPESAFE_DEFAULT_MODEL": "  "}), \
                patch("email_parser.jev.TypeSafeClient"):
            self.assertEqual(from_environment().model, "jev-latest")

    def test_invalid_configuration_is_safe_and_never_constructs_a_client(self):
        for model in ("", None, "jev\nsecret", "x" * 129):
            with patch("email_parser.jev.TypeSafeClient") as factory:
                with self.assertRaisesRegex(JevError, "Invalid TYPESAFE_DEFAULT_MODEL"):
                    JevClassifier("secret", model=model)
                factory.assert_not_called()
        for timeout in (0, -1, math.inf, math.nan, True, "10"):
            with patch("email_parser.jev.TypeSafeClient") as factory:
                with self.assertRaisesRegex(JevError, "timeout must be"):
                    JevClassifier("secret", timeout=timeout)
                factory.assert_not_called()
        with patch("email_parser.jev.TypeSafeClient", side_effect=ValueError("SECRET_CONFIGURATION")):
            with self.assertRaises(JevError) as caught:
                JevClassifier("secret")
        self.assertNotIn("SECRET_CONFIGURATION", str(caught.exception))

    def test_close_releases_sdk_and_redacts_any_failure(self):
        client = FakeClient()
        JevClassifier("test-key", client=client).close()
        self.assertTrue(client.closed)
        with patch.object(client, "close", side_effect=RuntimeError("SECRET_CLOSE")):
            with self.assertRaises(JevError) as caught:
                JevClassifier("test-key", client=client).close()
        self.assertNotIn("SECRET_CLOSE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
