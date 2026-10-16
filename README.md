# Email parser

Parse local `.eml` messages into JSON using Python's standard library. This project provides a command-line tool and a Python API, with no runtime dependencies, inbox access, credentials, or external services.

## Set up

Use [uv](https://docs.astral.sh/uv/) to install the project with the pinned Python 3.12 interpreter:

```sh
uv sync --frozen
```

Python 3.11 or later is supported. `uv` installs the build dependency, setuptools, as needed. Alternatively, create a virtual environment and install the package:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/email-parser examples/sample.eml --pretty
```

## Parse a message

```sh
uv run --frozen email-parser examples/sample.eml --pretty
```

JSON goes to standard output by default. Use `--output` (or `-o`) to create a JSON file; an existing file is never overwritten:

```sh
mkdir -p output
uv run --frozen email-parser examples/sample.eml --pretty -o output/parsed-email.json
```

Read standard input by omitting the path or supplying `-`:

```sh
uv run --frozen email-parser - --pretty < examples/sample.eml
```

The CLI accepts messages up to 25 MiB (26,214,400 bytes) by default. Set another positive byte limit with `--max-bytes`:

```sh
uv run --frozen email-parser examples/sample.eml --max-bytes 1048576
```

Text and HTML bodies remain separate strings. The parser does not render HTML or fetch remote content. Malformed MIME and charset problems produce best-effort output with warnings where recovery is possible.

## Attachments

JSON contains attachment metadata, including `filename`, `content_type`, decoded byte `size`, `content_id`, and `disposition`. Attachment contents are excluded from JSON.

Review the metadata first. To explicitly save attachment bytes after reviewing the message:

```sh
uv run --frozen email-parser examples/sample.eml --extract-attachments output/attachments
```

Extraction uses safe basenames and chooses unique filenames for collisions without overwriting existing files. Each saved attachment's metadata includes `extracted_path`, the absolute saved path. Extracted attachments are never executed. Treat their contents as untrusted. The `output/` directory is ignored by Git for local results.

## Python API

```python
from pathlib import Path

from email_parser import parse_email, parse_file

message = parse_file("examples/sample.eml")
result = message.to_dict()
print(result["subject"])
print(result["from"])

# Parse bytes already in memory.
message = parse_email(Path("examples/sample.eml").read_bytes())
attachment_bytes = message.attachments[0].data
```

Both functions return a `ParsedEmail`. Its `to_dict()` result includes:

- `subject`; address lists `from`, `to`, `cc`, `bcc`, and `reply_to`, with `name` and `address` for each entry.
- `date` as an ISO 8601 string or `null`, and `message_id`.
- `text`, `html`, and attachment metadata in `attachments`.
- `headers` as an ordered list of `name`/`value` objects, preserving duplicate headers, and `warnings`.

Attachment objects expose decoded bytes through `.data`. The API has no input size limit; callers should enforce a limit when processing untrusted or large inputs.

## Validate and build

```sh
uv run --frozen python -m unittest discover -s tests -v
uv build
```

The sample message contains fictional addresses, a Unicode subject, plain text and HTML alternatives, and a small `report.txt` attachment.
