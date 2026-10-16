# Inbox Atlas

A local email triage dashboard with a simulated mailbox. Browse important messages by category, understand their priority, and manage the inbox with persistent read, star, archive, and manual classification controls.

The mailbox is demo data. No real account is connected, and no Gmail, IMAP, or other live integration is implemented. Classification uses explainable rules, with no AI service, credentials, external requests, or runtime dependencies.

## Set up and run

Use [uv](https://docs.astral.sh/uv/) with the pinned Python 3.12 interpreter; Python 3.11 or later is supported:

```sh
uv sync --frozen
uv run --frozen email-inbox
```

The server defaults to address `127.0.0.1`, port `8000`, and SQLite database `output/inbox.sqlite3`. Open a browser on the same machine and enter `127.0.0.1:8000` in its address bar. Stop the server with Ctrl+C.

Choose another port or database when needed:

```sh
uv run --frozen email-inbox --port 8080 --db output/another-inbox.sqlite3
```

This is a single-user local prototype with no application login. It binds locally by default; use `--host 0.0.0.0` only when sharing on a trusted development network.

## Use the dashboard

- **Priority view** shows the three highest-ranked matching messages in each category. **All messages** shows every matching message. Categories cover Work, Finance, Personal, Travel, Shopping, Newsletters, Security, and Other.
- Select Inbox, Unread, Starred, or Archived; combine a category, priority filter, and search across sender, subject, and body. Overview counts describe the mailbox, independently of message filters.
- Open a message to read its full plain-text body, attachment metadata, and the reasons for its category and priority. Rules assign Critical, High, Normal, or Low priority, with higher scores appearing first. Promotional urgency is treated differently from action or security requests.
- Mark messages read or unread, star them, archive or restore them, and override their category or priority. Manual overrides are recorded and saved.
- **Simulate sync** adds the next batch from the demo provider. There are three batches; once exhausted, additional syncs add nothing. This never connects to an email account.
- **Reset demo** restores the original demo messages and its three sync batches after confirmation. Imported messages are preserved.
- **Import .eml** adds a local message through the same parser and classification pipeline. Imports are limited to 25 MiB; importing identical bytes again does not create another copy. Try `examples/sample.eml`.

The database is created automatically and seeded once. Messages, imports, overrides, flags, and sync progress survive browser refreshes and server restarts when using the same database. The `output/` directory is ignored by Git for local data.

Email HTML is converted to plain text for the dashboard rather than rendered. Attachments are shown as metadata; the dashboard does not execute them or fetch remote content.

## Parse .eml files into JSON

The standalone parser remains available without starting the dashboard:

```sh
uv run --frozen email-parser examples/sample.eml --pretty
uv run --frozen email-parser - --pretty < examples/sample.eml
```

JSON goes to standard output. Omitting the input path also reads stdin. Use `--output` (or `-o`) to create a new file; existing files are never overwritten:

```sh
mkdir -p output
uv run --frozen email-parser examples/sample.eml --pretty -o output/parsed-email.json
```

The CLI defaults to a 25 MiB input limit (26,214,400 bytes); `--max-bytes` accepts another positive byte limit. Text and HTML bodies remain separate in JSON. Malformed MIME or charset problems produce best-effort output with warnings where recovery is possible.

Attachment JSON contains `filename`, `content_type`, decoded byte `size`, `content_id`, and `disposition`, without content bytes. Review that metadata before explicitly extracting attachments:

```sh
uv run --frozen email-parser examples/sample.eml --extract-attachments output/attachments
```

Extraction uses safe basenames and unique names for collisions without overwriting files. Saved attachment metadata gains `extracted_path`, the absolute saved path. Attachments are never executed; treat their contents as untrusted.

## Python API

```python
from pathlib import Path

from email_parser import parse_email, parse_file

message = parse_file("examples/sample.eml")
print(message.to_dict()["subject"])

message = parse_email(Path("examples/sample.eml").read_bytes())
attachment_bytes = message.attachments[0].data
```

Both functions return `ParsedEmail`. Its `to_dict()` includes `subject`; address lists `from`, `to`, `cc`, `bcc`, and `reply_to` (each with `name` and `address`); ISO 8601 `date` or `null`; `message_id`; `text`; `html`; attachment metadata; ordered `headers` preserving duplicates; and `warnings`. Attachment objects expose decoded bytes through `.data`. The API imposes no size limit, so callers should enforce their own input limits.

## Architecture and future integrations

| Component | Responsibility |
| --- | --- |
| `parser.py`, `cli.py` | Decode MIME messages and provide the standalone JSON tool. |
| `triage.py` | Assign categories, scores, priorities, labels, and explanations using deterministic rules. |
| `demo.py` | Supply the initial simulated mailbox and finite sync batches. |
| `store.py` | Ingest messages and persist inbox state in SQLite. |
| `server.py` | Serve the dashboard and JSON API using Python's HTTP server. |
| `static/` | Provide the browser interface with local HTML, CSS, and JavaScript. |

A future Gmail or IMAP adapter can supply raw MIME messages to the same ingestion path used by demo sync and `.eml` imports. Live authentication, account connection, and provider synchronization are deferred.

## Validate and build

```sh
uv run --frozen python -m unittest discover -s tests -v
uv build
```

The build includes the dashboard's static assets. Setuptools is a build dependency managed by `uv`; the running application uses only Python's standard library and browser APIs.
