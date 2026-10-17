# Inbox Atlas

A local email triage dashboard with Gmail sign-in, a simulated mailbox, and TypeSafe AI's hosted Jev classifier. Browse important messages by category and manage multiple mailboxes with persistent read, star, archive, and manual classification controls.

Start with the demo mailbox, or configure Google OAuth and explicitly connect Gmail. Gmail access is read-only; the app's flags and classification choices stay local. Jev classification requires a separate server-side API key. Without it, the dashboard remains usable with rule-based results explicitly marked as previews awaiting Jev. No connector plugin is required.

## Set up and run

Use [uv](https://docs.astral.sh/uv/) with the pinned Python 3.12 interpreter; Python 3.11 or later is supported:

```sh
uv sync --frozen
uv run --frozen email-inbox
```

The server defaults to address `127.0.0.1` and port `8000`. In a source checkout, its SQLite database is the checkout's `output/inbox.sqlite3`, independent of the shell's working directory. Open a browser on the same machine and enter `127.0.0.1:8000` in its address bar. Stop the server with Ctrl+C.

Choose another port or database when needed:

```sh
uv run --frozen email-inbox --port 8080 --db output/another-inbox.sqlite3
```

`INBOX_DATA_DIR` optionally chooses a stable data directory, with `inbox.sqlite3` inside it. Outside a source checkout, an installed package defaults to the platform's user application data directory under `inbox-atlas`. `--db` overrides either default.

This is a single-user local prototype with no application login. It binds locally by default; use `--host 0.0.0.0` only when sharing on a trusted development network.

## Connect Gmail

Create your own Google OAuth client before connecting an account:

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project and enable the **Gmail API** from the API Library.
2. Configure the OAuth consent screen in Google Auth Platform: provide the app name and contact details, choose the appropriate audience, and add the Google accounts you will connect as **test users** if the app is External and in Testing.
3. Add the Gmail read-only scope, `https://www.googleapis.com/auth/gmail.readonly`, to the app's data access configuration.
4. Create an OAuth client of type **Web application**. Add this exact **Authorized redirect URI**:

   ```text
   http://127.0.0.1:8000/auth/gmail/callback
   ```

5. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the server's secure environment settings, then restart the app. For local development, securely populate the shell variables and export them with `export GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET`. Keep these values out of browser forms, source files, and chat.
6. Open the app using the same configured origin, choose **Connect Gmail**, and complete Google's sign-in and consent flow. The app imports messages in the background after consent; connection success does not mean import is already complete.

`INBOX_GOOGLE_REDIRECT_URI` optionally overrides the callback. If you change the port or use an HTTPS development origin, register the full matching callback with Google and set this variable to that exact URI. Start sign-in from the same origin; `localhost` and `127.0.0.1` are different origins. HTTP callbacks are supported only for loopback development addresses; external callback origins require HTTPS.

The OAuth scope grants read-only Gmail access. On a **new connection**, the app imports incoming messages from the preceding **72 hours**, using Gmail's internal receipt time. Later syncs use Gmail history to fetch new mail and changes; the initial three-day cutoff is not reapplied on every sync. Sent messages, drafts, spam, and trash are excluded. Connected accounts poll automatically about once a minute; **Sync now** requests an immediate sync.

Use the account selector to switch between Demo and linked Gmail accounts. Filters and inbox state are stored separately per account. **Reconnect** restores an existing connection without resetting its import window or saved choices. **Disconnect** removes that account's local tokens and stops sync while retaining its cached messages and local state.

Gmail sign-in has not yet been verified against a live account in this environment because Google OAuth client configuration is absent. It requires the configured client and the user's Google consent flow.

## Configure Jev

Set `TYPESAFE_API_KEY` in the cloud environment's secure secret settings, then restart the server. For local use, securely populate the shell variable and run `export TYPESAFE_API_KEY` before starting the app. Keep the value on the server; it is never entered into the browser or saved in source files. `TYPESAFE_DEFAULT_MODEL` optionally selects another model; the default is `jev-latest`.

The backend uses the official `typesafe-sdk==0.7.5` package to call `https://api.typesafe.ai/v1/systemone`. It sends message subject, sender, recipients, received date, plain-text body (up to 12,000 characters), and attachment names and content types. It does not send attachment bytes or email HTML. The browser communicates only with this application's server.

Jev returns a typed category choice and a priority score over five rubric levels: no attention/promotional, low routine, normal, high actionable, and critical immediate risk. The app derives its labels, numeric ranking, and four displayed priorities from these decisions. Confidence is separate from urgency. Displayed decision summaries describe the app's rubric; they are not model-generated explanations.

Existing nonmanual messages are processed in the background when the server starts. New `.eml` imports, simulated sync messages, and downloaded Gmail messages join the same queue. Configuring Jev therefore sends the bounded message data described above from linked mailboxes to TypeSafe for classification. The Jev status panel shows configuration, progress, and failures; manual category or priority overrides are preserved and excluded from automatic processing.

If the key is missing, results remain clearly labeled previews. A provider error marks the affected classification as failed and pauses processing until **Retry**. Retry resumes failed and pending work; **Reclassify** requests a fresh pass over nonmanual messages, including those already completed. Restart the server after changing its API key or model setting.

## Use the dashboard

- **Priority view** shows the three highest-ranked matching messages in each category. **All messages** shows every matching message. Categories cover Work, Finance, Personal, Travel, Shopping, Newsletters, Security, and Other.
- Select Inbox, Unread, Starred, or Archived; combine a category, priority filter, and search across sender, subject, and body. Overview counts describe the mailbox, independently of message filters.
- Open a message to read its full plain-text body, attachment metadata, classification status, and category/priority summary. Priorities are Critical, High, Normal, or Low, with higher scores appearing first. Pending previews and failed classifications remain visibly distinct from completed Jev results.
- Opening a message automatically marks it as read. Marking it unread while it remains open is preserved until it is reopened. Star messages, archive or restore them, and override their category or priority; every change is saved. These actions affect the local app and do not change Gmail labels or read status. Future mail syncs preserve local choices.
- **Simulate sync** adds the next batch from the demo provider. There are three batches; once exhausted, additional syncs add nothing. This never connects to an email account.
- **Reset demo** restores the original demo messages and its three sync batches after confirmation. Imported messages and linked Gmail mailboxes are preserved. This action is available only in Demo.
- **Import .eml** adds a local message through the same parser and classification pipeline. Imports are limited to 25 MiB; importing identical bytes again does not create another copy. New messages appear immediately as previews while Jev processes them when configured. Try `examples/sample.eml`.

The database is created automatically and seeded once. Messages, imports, classification decisions, read/star/archive flags, selected account, per-account filters, and sync progress survive browser refreshes and server restarts when using the same database. Existing demo databases are migrated additively, preserving their messages and choices. The `output/` directory is ignored by Git for local data.

Email HTML is converted to plain text for the dashboard rather than rendered. Attachments are shown as metadata; the dashboard does not execute them or fetch remote content.

## Keep and back up local state

The default source-checkout state files are `output/inbox.sqlite3` and, after Gmail setup, `output/.inbox-token-key`. With another data directory, the key still lives beside the database. OAuth access and refresh tokens are encrypted in SQLite using that locally generated key. Credentials and tokens stay on the server; the browser stores no tokens or message bodies in local storage.

Stop the server before copying these files and back up the database **together with its original key**. Restore both together. Keep the key private and do not delete or regenerate either file as an upgrade step: the key is required to decrypt existing Gmail tokens. When using `--db`, the key lives beside the chosen database. Selecting another database starts a separate state store.

Use `--no-mail` to disable Gmail sign-in and synchronization for the session while retaining stored state:

```sh
uv run --frozen email-inbox --no-mail
```

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
| `gmail.py`, `gmail_auth.py` | Use Gmail's REST API and OAuth authorization-code/PKCE flow with encrypted token storage. |
| `mailbox.py` | Backfill new connections and run incremental Gmail synchronization. |
| `demo.py` | Supply the initial simulated mailbox and finite sync batches. |
| `store.py` | Ingest messages and persist inbox state in SQLite. |
| `server.py` | Serve the dashboard and JSON API using Python's HTTP server. |
| `static/` | Provide the browser interface with local HTML, CSS, and JavaScript. |

Gmail sync, demo sync, and `.eml` imports share the raw MIME ingestion path. IMAP and other providers can later supply messages through the same interface.

## Validate and build

```sh
uv run --frozen python -m unittest discover -s tests -v
uv build
```

The build includes the dashboard's static assets. Setuptools is a build dependency managed by `uv`; the backend uses Python's standard HTTP/SQLite facilities, the official TypeSafe SDK, and `cryptography` for token encryption, with plain browser APIs for the interface.

Automated tests use fake classifier/SDK and Gmail/OAuth transports, without reading real mail or making live provider calls. Live Gmail validation still requires Google OAuth configuration and browser consent.
