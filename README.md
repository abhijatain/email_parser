# Inbox Atlas

A local email triage dashboard with a simulated mailbox and TypeSafe AI's hosted Jev classifier. Browse important messages by category and manage the inbox with persistent read, star, archive, and manual classification controls.

The mailbox is demo data. No real account is connected, and no Gmail, IMAP, or other live mailbox integration is implemented. Jev classification requires a server-side API key. Without it, the dashboard remains usable with rule-based results explicitly marked as previews awaiting Jev.

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

## Configure Jev

Set `TYPESAFE_API_KEY` in the cloud environment's secure secret settings, then restart the server. For local use, securely populate the shell variable and run `export TYPESAFE_API_KEY` before starting the app. Keep the value on the server; it is never entered into the browser or saved in source files. `TYPESAFE_DEFAULT_MODEL` optionally selects another model; the default is `jev-latest`.

The backend uses the official `typesafe-sdk==0.7.5` package to call `https://api.typesafe.ai/v1/systemone`. It sends message subject, sender, recipients, received date, plain-text body (up to 12,000 characters), and attachment names and content types. It does not send attachment bytes or email HTML. The browser communicates only with this application's server.

Jev returns a typed category choice and a priority score over five rubric levels: no attention/promotional, low routine, normal, high actionable, and critical immediate risk. The app derives its labels, numeric ranking, and four displayed priorities from these decisions. Confidence is separate from urgency. Displayed decision summaries describe the app's rubric; they are not model-generated explanations.

Existing nonmanual messages are processed in the background when the server starts. New imports and simulated sync messages join the same queue. The Jev status panel shows configuration, progress, and failures; manual category or priority overrides are preserved and excluded from automatic processing.

If the key is missing, results remain clearly labeled previews. A provider error marks the affected classification as failed and pauses processing until **Retry**. Retry resumes failed and pending work; **Reclassify** requests a fresh pass over nonmanual messages, including those already completed. Restart the server after changing its API key or model setting.

## Use the dashboard

- **Priority view** shows the three highest-ranked matching messages in each category. **All messages** shows every matching message. Categories cover Work, Finance, Personal, Travel, Shopping, Newsletters, Security, and Other.
- Select Inbox, Unread, Starred, or Archived; combine a category, priority filter, and search across sender, subject, and body. Overview counts describe the mailbox, independently of message filters.
- Open a message to read its full plain-text body, attachment metadata, classification status, and category/priority summary. Priorities are Critical, High, Normal, or Low, with higher scores appearing first. Pending previews and failed classifications remain visibly distinct from completed Jev results.
- Mark messages read or unread, star them, archive or restore them, and override their category or priority. Manual overrides are recorded and saved.
- **Simulate sync** adds the next batch from the demo provider. There are three batches; once exhausted, additional syncs add nothing. This never connects to an email account.
- **Reset demo** restores the original demo messages and its three sync batches after confirmation. Imported messages are preserved.
- **Import .eml** adds a local message through the same parser and classification pipeline. Imports are limited to 25 MiB; importing identical bytes again does not create another copy. New messages appear immediately as previews while Jev processes them when configured. Try `examples/sample.eml`.

The database is created automatically and seeded once. Messages, imports, overrides, flags, and sync progress survive browser refreshes and server restarts when using the same database. The `output/` directory is ignored by Git for local data.

Email HTML is converted to plain text for the dashboard rather than rendered. Attachments are shown as metadata; the dashboard does not execute them or fetch remote content.

## Parse .eml files into JSON

The standalone parser remains offline and available without starting the dashboard or configuring Jev:

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
| `triage.py` | Provide deterministic rule-based previews before Jev classification. |
| `jev.py` | Send bounded message data to the official SDK and validate typed Jev decisions. |
| `classification.py` | Queue background classification, track progress, and preserve manual overrides. |
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

The build includes the dashboard's static assets. Setuptools is a build dependency managed by `uv`; the backend uses Python's standard HTTP/SQLite facilities and the official TypeSafe SDK, with plain browser APIs for the interface.

Automated tests use fake classifier/SDK adapters and make no real TypeSafe API calls. Live Jev validation requires a configured `TYPESAFE_API_KEY` and network access to `api.typesafe.ai`; it has not been verified in the current environment without that credential.
