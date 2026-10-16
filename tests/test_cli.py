"""End-to-end CLI checks including bounded input and safe file writes."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SIMPLE_EMAIL = b"From: sender@example.com\r\nSubject: Example\r\n\r\nHello world\r\n"


class CliTests(unittest.TestCase):
    def run_cli(self, *args, data=None):
        env = os.environ.copy()
        source_path = str(REPO_ROOT / "src")
        env["PYTHONPATH"] = source_path + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "email_parser", *map(str, args)],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=REPO_ROOT, env=env, timeout=20, check=False,
        )

    def test_stdin_defaults_to_json_stdout(self):
        result = self.run_cli(data=SIMPLE_EMAIL)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b"")
        document = json.loads(result.stdout)
        self.assertEqual(document["subject"], "Example")
        self.assertEqual(document["text"].strip(), "Hello world")
        self.assertEqual(document["from"][0]["address"], "sender@example.com")

    def test_explicit_stdin_and_pretty_json(self):
        result = self.run_cli("-", "--pretty", data=SIMPLE_EMAIL)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"\n", result.stdout.rstrip())
        self.assertEqual(json.loads(result.stdout)["subject"], "Example")

    def test_file_input_and_output_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "mail.eml"
            output = Path(temp_dir) / "parsed.json"
            source.write_bytes(SIMPLE_EMAIL)

            result = self.run_cli(source, "--output", output)

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout, b"")
            self.assertEqual(json.loads(output.read_text())["subject"], "Example")

    def test_missing_input_is_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "does-not-exist.eml"
            result = self.run_cli(source)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"does-not-exist.eml", result.stderr)

    def test_size_limit_applies_to_stdin_and_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "mail.eml"
            source.write_bytes(SIMPLE_EMAIL)
            for args, data in [(("-",), SIMPLE_EMAIL), ((source,), None)]:
                with self.subTest(input=args):
                    result = self.run_cli(*args, "--max-bytes", "16", data=data)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, b"")
                    self.assertTrue(result.stderr)

    def test_size_limit_must_be_positive(self):
        for limit in ("0", "-1", "nonsense"):
            with self.subTest(limit=limit):
                result = self.run_cli("--max-bytes", limit, data=SIMPLE_EMAIL)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr)

    def test_oversized_byte_limit_is_a_clean_argument_error(self):
        for limit in (sys.maxsize, sys.maxsize + 1):
            with self.subTest(limit=limit):
                result = self.run_cli("--max-bytes", limit, data=SIMPLE_EMAIL)

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)

    def test_large_valid_byte_limit_does_not_allocate_the_limit(self):
        result = self.run_cli("--max-bytes", sys.maxsize - 1, data=SIMPLE_EMAIL)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)["subject"], "Example")

    def test_excessive_mime_nesting_fails_without_traceback_or_partial_json(self):
        depth = 1200
        opening_parts = [
            f"Content-Type: multipart/mixed; boundary=b{i}\r\n\r\n--b{i}\r\n".encode()
            for i in range(depth)
        ]
        closing_parts = [f"--b{i}--\r\n".encode() for i in reversed(range(depth))]
        raw = b"".join(opening_parts) + b"Content-Type: text/plain\r\n\r\nleaf\r\n" + b"".join(closing_parts)

        result = self.run_cli(data=raw)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_output_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "existing.json"
            output.write_text("keep this content")

            result = self.run_cli("-o", output, data=SIMPLE_EMAIL)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"")
            self.assertTrue(result.stderr)
            self.assertEqual(output.read_text(), "keep this content")

    def test_attachment_paths_are_contained_and_collisions_preserve_all_payloads(self):
        message = EmailMessage()
        message.set_content("Attachments below")
        attachments = [
            ("../../escape.txt", b"outside attempt"),
            ("C:\\outside\\windows.txt", b"windows attempt"),
            ("duplicate.txt", b"first duplicate"),
            ("duplicate.txt", b"second duplicate"),
        ]
        for filename, payload in attachments:
            message.add_attachment(payload, maintype="application", subtype="octet-stream", filename=filename)
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "extracted"

            result = self.run_cli("--extract-attachments", destination, data=message.as_bytes())

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            document = json.loads(result.stdout)
            saved_paths = []
            for metadata in document["attachments"]:
                self.assertNotIn("data", metadata)
                saved = Path(metadata["extracted_path"])
                if not saved.is_absolute():
                    saved = REPO_ROOT / saved
                saved = saved.resolve()
                self.assertTrue(saved.is_relative_to(destination.resolve()))
                self.assertEqual(saved.parent, destination.resolve())
                saved_paths.append(saved)
            self.assertEqual(len(set(saved_paths)), len(attachments))
            self.assertEqual({p.read_bytes() for p in saved_paths}, {data for _, data in attachments})
            self.assertFalse((Path(temp_dir) / "escape.txt").exists())

    def test_existing_attachment_is_never_overwritten(self):
        message = EmailMessage()
        message.set_content("hello")
        message.add_attachment(b"new content", maintype="application", subtype="octet-stream", filename="report.txt")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "extracted"
            destination.mkdir()
            original = destination / "report.txt"
            original.write_bytes(b"existing content")

            result = self.run_cli("--extract-attachments", destination, data=message.as_bytes())

            self.assertEqual(original.read_bytes(), b"existing content")
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            document = json.loads(result.stdout)
            saved = Path(document["attachments"][0]["extracted_path"])
            if not saved.is_absolute():
                saved = REPO_ROOT / saved
            self.assertNotEqual(saved.resolve(), original.resolve())
            self.assertEqual(saved.read_bytes(), b"new content")

    def test_file_output_with_attachments_emits_no_stdout(self):
        message = EmailMessage()
        message.set_content("hello")
        message.add_attachment(b"attachment content", maintype="application", subtype="octet-stream", filename="report.txt")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "attachments"
            output = Path(temp_dir) / "parsed.json"

            result = self.run_cli("-o", output, "--extract-attachments", destination, data=message.as_bytes())

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout, b"")
            document = json.loads(output.read_text())
            saved = Path(document["attachments"][0]["extracted_path"])
            if not saved.is_absolute():
                saved = REPO_ROOT / saved
            self.assertTrue(saved.resolve().is_relative_to(destination.resolve()))
            self.assertEqual(saved.read_bytes(), b"attachment content")

    def test_existing_output_failure_creates_no_attachment_files(self):
        message = EmailMessage()
        message.set_content("hello")
        message.add_attachment(b"new content", maintype="application", subtype="octet-stream", filename="report.txt")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "extracted"
            output = Path(temp_dir) / "parsed.json"
            output.write_text("original")

            result = self.run_cli("-o", output, "--extract-attachments", destination, data=message.as_bytes())

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(output.read_text(), "original")
            self.assertFalse(destination.exists() and any(destination.iterdir()))

    def test_attachment_extraction_does_not_follow_existing_file_symlinks(self):
        message = EmailMessage()
        message.set_content("hello")
        message.add_attachment(b"new content", maintype="application", subtype="octet-stream", filename="report.txt")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "extracted"
            destination.mkdir()
            original = Path(temp_dir) / "outside.txt"
            original.write_bytes(b"original outside content")
            link = destination / "report.txt"
            link.symlink_to(original)

            result = self.run_cli("--extract-attachments", destination, data=message.as_bytes())

            self.assertEqual(original.read_bytes(), b"original outside content")
            self.assertTrue(link.is_symlink())
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            document = json.loads(result.stdout)
            saved = Path(document["attachments"][0]["extracted_path"])
            if not saved.is_absolute():
                saved = REPO_ROOT / saved
            self.assertTrue(saved.resolve().is_relative_to(destination.resolve()))
            self.assertEqual(saved.read_bytes(), b"new content")


if __name__ == "__main__":
    unittest.main()
