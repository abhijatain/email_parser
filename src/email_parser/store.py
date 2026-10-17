"""Persistent local inbox with a provider-independent ingestion pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from .demo import BATCH_COUNT, messages_for_batch
from .parser import parse_email
from .triage import CATEGORIES, PRIORITIES, classify_email


_ACCOUNT_PUBLIC_FIELDS = (
    "id", "name", "email", "provider", "mode", "connected", "status",
    "last_synced_at", "initial_import_complete", "error",
)
_PREFERENCE_DEFAULTS = {
    "view": "inbox", "mode": "priority", "category": "", "priority": "", "search": "",
}


def account_public(account: dict) -> dict:
    """Explicit public allowlist: credentials and sync internals never escape."""
    return {field: account.get(field) for field in _ACCOUNT_PUBLIC_FIELDS}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class _PlainHTML(HTMLParser):
    """Extract readable text, discarding markup and non-content elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "head", "template"}:
            self.ignored.append(tag)
        if not self.ignored and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def _plain_body(text: str, html: str) -> str:
    if text.strip():
        return text.strip()
    parser = _PlainHTML()
    parser.feed(html)
    parser.close()
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _received_at(value: str | None, fallback: datetime) -> str:
    try:
        parsed = datetime.fromisoformat(value) if value else fallback
    except ValueError:
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class InboxStore:
    """SQLite-backed inbox safe to share between HTTP request threads.

    Each operation owns its connection. Write transactions also acquire a
    process-local lock; BEGIN IMMEDIATE protects seed/sync state even if a
    second store instance opens the same database.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self.db_path.chmod(0o600)
        self._lock = threading.RLock()
        with self._connection(write=True) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, source TEXT NOT NULL, payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, payload TEXT NOT NULL, token_blob TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS preferences (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS auth_states (state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, "
                "payload TEXT NOT NULL, expires_at REAL NOT NULL)"
            )
            if self._account(connection, "demo") is None:
                self._put_account(connection, {
                    "id": "demo", "name": "Alex Morgan", "email": "alex.morgan@example.com",
                    "provider": "demo", "mode": "demo", "connected": False, "status": "demo",
                    "last_synced_at": None, "initial_import_complete": True, "error": None,
                    "history_id": None, "generation": uuid.uuid4().hex,
                    "initial_since": None, "initial_before": None,
                })
            connection.execute("INSERT OR IGNORE INTO state (key,value) VALUES ('active_account_id','demo')")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS classification_jobs ("
                "message_id TEXT PRIMARY KEY, token TEXT NOT NULL, state TEXT NOT NULL, "
                "model TEXT NOT NULL, rubric TEXT NOT NULL)"
            )
            seeded = connection.execute("SELECT value FROM state WHERE key = 'seeded_at'").fetchone()
            if seeded is None:
                self._seed(connection, _utc_now())
            # Older inboxes contain rule outputs without attribution. Migration
            # changes only their provenance; existing categories and flags stay.
            for message in self._rows(connection):
                changed = False
                if "classification" not in message:
                    message["classification"] = self._annotation(message)
                    changed = True
                for key, default in (("account_id", "demo"), ("local_flags", {})):
                    if key not in message:
                        message[key] = default
                        changed = True
                if changed:
                    self._put(connection, message)

    @contextmanager
    def _connection(self, *, write: bool = False):
        with self._lock:
            connection = sqlite3.connect(self.db_path, timeout=20)
            try:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
                if write:
                    connection.commit()
            except BaseException:
                if write:
                    connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def _put(connection: sqlite3.Connection, message: dict) -> None:
        connection.execute(
            "INSERT INTO messages (id, source, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, source=excluded.source",
            (message["id"], message["source"], json.dumps(message, ensure_ascii=False)),
        )

    @staticmethod
    def _message(data: bytes, message_id: str, source: str, now: datetime,
                 *, unread: bool = True, starred: bool = False) -> dict:
        parsed = parse_email(data)
        text = _plain_body(parsed.text, parsed.html)
        classification = classify_email(replace(parsed, text=text, html=""), now=now)
        sender = parsed.from_[0].to_dict() if parsed.from_ else {"name": "Unknown sender", "address": ""}
        return {
            "id": message_id, "subject": parsed.subject or "(No subject)",
            "sender": sender, "to": [address.to_dict() for address in parsed.to],
            "received_at": _received_at(parsed.date, now), "text": text,
            "preview": re.sub(r"\s+", " ", text).strip()[:180],
            **classification, "unread": unread, "starred": starred, "archived": False,
            "attachments": [attachment.to_dict() for attachment in parsed.attachments],
            "source": source, "overridden": False,
            "classification": {"provider": "rules", "status": "preview"},
            "account_id": "demo", "local_flags": {},
        }

    @staticmethod
    def _annotation(message: dict) -> dict:
        if message.get("overridden"):
            return {"provider": "manual", "status": "manual"}
        annotation = message.get("classification")
        return annotation if isinstance(annotation, dict) else {"provider": "rules", "status": "preview"}

    def _seed(self, connection: sqlite3.Connection, now: datetime) -> None:
        for demo in messages_for_batch(0, now):
            self._put(connection, self._message(demo.data, f"demo-{demo.key}", "demo", now,
                                               unread=demo.unread, starred=demo.starred))
        connection.execute("INSERT OR REPLACE INTO state (key,value) VALUES ('seeded_at',?)", (now.isoformat(),))
        connection.execute("INSERT OR REPLACE INTO state (key,value) VALUES ('demo_batch','0')")

    @staticmethod
    def _rows(connection: sqlite3.Connection) -> list[dict]:
        return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM messages")]

    def list_messages(self, search: str = "", category: str = "", priority: str = "",
                      view: str = "inbox", account_id: str = "demo") -> list[dict]:
        if any(not isinstance(value, str) for value in (search, category, priority, view)):
            raise ValueError("Inbox filters must be strings")
        if category and category not in {item["id"] for item in CATEGORIES}:
            raise ValueError("Unknown category")
        if priority and priority not in PRIORITIES:
            raise ValueError("Unknown priority")
        if view not in {"inbox", "unread", "starred", "archived"}:
            raise ValueError("Unknown inbox view")
        with self._connection() as connection:
            self._require_account(connection, account_id)
            messages = self._rows(connection)
        query = search.casefold().strip()
        result = []
        for message in messages:
            if message.get("account_id", "demo") != account_id:
                continue
            if message["archived"] != (view == "archived"):
                continue
            if view in {"unread", "starred"} and not message[view]:
                continue
            if category and message["category"] != category:
                continue
            if priority and message["priority"] != priority:
                continue
            searchable = "\n".join((message["subject"], message["sender"]["name"],
                                     message["sender"]["address"], message["text"]))
            if query and query not in searchable.casefold():
                continue
            result.append(message)
        # Stable ID is the last tie-breaker. ISO timestamps are normalized UTC.
        result.sort(key=lambda message: message["id"])
        result.sort(key=lambda message: message["received_at"], reverse=True)
        result.sort(key=lambda message: message["score"], reverse=True)
        return result

    def get_message(self, message_id: str, account_id: str | None = None) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT payload FROM messages WHERE id = ?", (message_id,)).fetchone()
            message = json.loads(row[0]) if row else None
            return message if message and (account_id is None or message.get("account_id", "demo") == account_id) else None

    def update_message(self, message_id: str, changes: dict, account_id: str | None = None) -> dict | None:
        if not isinstance(changes, dict):
            raise ValueError("Changes must be a JSON object")
        allowed = {"unread", "starred", "archived", "category", "priority"}
        if set(changes) - allowed:
            raise ValueError("Unknown message field")
        for key in ("unread", "starred", "archived"):
            if key in changes and type(changes[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        if "category" in changes and (
            not isinstance(changes["category"], str)
            or changes["category"] not in {item["id"] for item in CATEGORIES}
        ):
            raise ValueError("Unknown category")
        if "priority" in changes and (
            not isinstance(changes["priority"], str) or changes["priority"] not in PRIORITIES
        ):
            raise ValueError("Unknown priority")
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT payload FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None:
                return None
            message = json.loads(row[0])
            if account_id is not None and message.get("account_id", "demo") != account_id:
                return None
            message.update(changes)
            for flag in ("unread", "starred", "archived"):
                if flag in changes:
                    message.setdefault("local_flags", {})[flag] = True
            if changes.get("unread") is False:
                message["last_read_at"] = _utc_now().isoformat()
            if "category" in changes:
                names = {item["name"] for item in CATEGORIES}
                name = next(item["name"] for item in CATEGORIES if item["id"] == changes["category"])
                message["labels"] = [label for label in message["labels"] if label not in names and label != "Category override"]
                message["labels"] = [name, *message["labels"], "Category override"]
                message["reasons"] = [reason for reason in message["reasons"] if not reason.startswith("Category manually set to ")]
                message["reasons"].append(f"Category manually set to {name}.")
                message["overridden"] = True
            if "priority" in changes:
                priority = changes["priority"]
                message["score"] = {"critical": 92, "high": 75, "normal": 50, "low": 20}[priority]
                if "Priority override" not in message["labels"]:
                    message["labels"].append("Priority override")
                message["reasons"] = [reason for reason in message["reasons"] if not reason.startswith("Priority manually set to ")]
                message["reasons"].append(f"Priority manually set to {priority}; the ranking uses your selection.")
                message["overridden"] = True
            if "category" in changes or "priority" in changes:
                message["classification"] = {"provider": "manual", "status": "manual"}
                message["labels"] = [label for label in message["labels"] if label != "Jev AI"]
                connection.execute("DELETE FROM classification_jobs WHERE message_id = ?", (message_id,))
            self._put(connection, message)
            return message

    def summary(self, account_id: str = "demo") -> dict:
        with self._connection() as connection:
            self._require_account(connection, account_id)
            messages = [message for message in self._rows(connection) if message.get("account_id", "demo") == account_id]
            batch = int(connection.execute("SELECT value FROM state WHERE key = 'demo_batch'").fetchone()[0])
        active = [message for message in messages if not message["archived"]]
        categories = []
        for category in CATEGORIES:
            grouped = [message for message in active if message["category"] == category["id"]]
            categories.append({"id": category["id"], "name": category["name"], "count": len(grouped),
                               "unread": sum(message["unread"] for message in grouped),
                               "important": sum(message["priority"] in {"critical", "high"} for message in grouped)})
        return {"total": len(active), "unread": sum(message["unread"] for message in active),
                "important": sum(message["priority"] in {"critical", "high"} for message in active),
                "starred": sum(message["starred"] for message in active),
                "archived": len(messages) - len(active), "categories": categories,
                "demo_batches_remaining": max(0, BATCH_COUNT - batch) if account_id == "demo" else 0}

    def sync_demo(self) -> dict:
        with self._connection(write=True) as connection:
            batch = int(connection.execute("SELECT value FROM state WHERE key = 'demo_batch'").fetchone()[0])
            if batch >= BATCH_COUNT:
                return {"added": 0, "remaining_batches": 0}
            now = _utc_now()
            demos = messages_for_batch(batch + 1, now)
            for demo in demos:
                self._put(connection, self._message(demo.data, f"demo-{demo.key}", "demo", now,
                                                   unread=demo.unread, starred=demo.starred))
            connection.execute("UPDATE state SET value = ? WHERE key = 'demo_batch'", (str(batch + 1),))
            return {"added": len(demos), "remaining_batches": BATCH_COUNT - batch - 1}

    def reset_demo(self) -> None:
        with self._connection(write=True) as connection:
            # A reset reuses demo IDs. Drop their job tokens before reseeding so
            # a response for an older instance cannot annotate the new message.
            demo_ids = [message["id"] for message in self._rows(connection)
                        if message["source"] == "demo" and message.get("account_id", "demo") == "demo"]
            connection.executemany("DELETE FROM classification_jobs WHERE message_id = ?", ((item,) for item in demo_ids))
            connection.executemany("DELETE FROM messages WHERE id = ?", ((item,) for item in demo_ids))
            self._seed(connection, _utc_now())

    def import_email(self, data: bytes, account_id: str = "demo") -> dict:
        if not isinstance(data, bytes):
            raise ValueError("Email input must be bytes")
        prefix = "import-" if account_id == "demo" else f"{account_id}-import-"
        message_id = prefix + hashlib.sha256(data).hexdigest()
        with self._connection(write=True) as connection:
            self._require_account(connection, account_id)
            row = connection.execute("SELECT payload FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row:
                return json.loads(row[0])
            message = self._message(data, message_id, "import", _utc_now())
            message["account_id"] = account_id
            self._put(connection, message)
            return message

    @staticmethod
    def _account(connection: sqlite3.Connection, account_id: str) -> dict | None:
        if not isinstance(account_id, str):
            return None
        row = connection.execute("SELECT payload FROM accounts WHERE id=?", (account_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @classmethod
    def _require_account(cls, connection: sqlite3.Connection, account_id: str) -> dict:
        account = cls._account(connection, account_id)
        if account is None:
            raise ValueError("Unknown mailbox account")
        return account

    @staticmethod
    def _put_account(connection: sqlite3.Connection, account: dict) -> None:
        connection.execute(
            "INSERT INTO accounts (id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (account["id"], json.dumps(account, ensure_ascii=False)),
        )

    @staticmethod
    def _generation_matches(account: dict | None, expected_generation: str) -> bool:
        return bool(account and account.get("connected") and account.get("generation") == expected_generation)

    def list_accounts(self) -> list[dict]:
        with self._connection() as connection:
            accounts = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM accounts ORDER BY id")]
        accounts.sort(key=lambda account: (account["id"] != "demo", account["email"].casefold(), account["id"]))
        return [account_public(account) for account in accounts]

    def get_account(self, account_id: str) -> dict | None:
        with self._connection() as connection:
            return self._account(connection, account_id)

    def get_account_tokens(self, account_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT token_blob FROM accounts WHERE id=?", (account_id,)).fetchone()
            return row[0] if row else None

    def upsert_gmail_account(self, profile: dict, token_blob: str, now: datetime | None = None,
                             *, expected_generation: str | None = None) -> dict | None:
        email = profile.get("emailAddress") if isinstance(profile, dict) else None
        if not isinstance(email, str):
            raise ValueError("Gmail did not return a valid account email")
        email = email.strip().casefold()
        if (len(email) > 320 or email.count("@") != 1 or not all(email.split("@"))
                or any(character.isspace() or ord(character) < 32 for character in email)):
            raise ValueError("Gmail did not return a valid account email")
        if not isinstance(token_blob, str) or not token_blob:
            raise ValueError("Encrypted account credentials are required")
        instant = _utc(now or _utc_now())
        account_id = "gmail-" + hashlib.sha256(email.encode("utf-8")).hexdigest()
        with self._connection(write=True) as connection:
            account = self._account(connection, account_id)
            # OAuth network operations run outside transactions. Commit only
            # if no disconnect or competing sign-in changed the begin-time
            # account. The empty sentinel protects a genuinely new connection.
            current_generation = account.get("generation") if account is not None else ""
            if expected_generation is not None and current_generation != expected_generation:
                return None
            if account is None:
                account = {
                    "id": account_id, "name": email.split("@", 1)[0], "email": email,
                    "provider": "gmail", "mode": "gmail", "last_synced_at": None,
                    "initial_import_complete": False, "history_id": None,
                    "initial_history_id": profile.get("historyId") if isinstance(profile.get("historyId"), str) else None,
                    "initial_since": (instant - timedelta(hours=72)).isoformat(),
                    "initial_before": instant.isoformat(),
                }
            account.update({"connected": True, "status": "connected", "error": None,
                            "generation": uuid.uuid4().hex})
            self._put_account(connection, account)
            connection.execute("UPDATE accounts SET token_blob=? WHERE id=?", (token_blob, account_id))
            return account

    def set_account_tokens(self, account_id: str, blob: str, expected_generation: str) -> bool:
        if not isinstance(blob, str) or not blob:
            raise ValueError("Encrypted account credentials are required")
        with self._connection(write=True) as connection:
            account = self._account(connection, account_id)
            if not self._generation_matches(account, expected_generation):
                return False
            connection.execute("UPDATE accounts SET token_blob=? WHERE id=?", (blob, account_id))
            return True

    def update_account_sync(self, account_id: str, changes: dict, expected_generation: str) -> bool:
        allowed = {"history_id", "initial_history_id", "initial_import_complete", "last_synced_at", "status", "error"}
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise ValueError("Unknown mailbox sync field")
        updates = dict(changes)
        if "initial_import_complete" in updates and type(updates["initial_import_complete"]) is not bool:
            raise ValueError("Initial import state must be a boolean")
        for field in ("history_id", "initial_history_id", "error"):
            if field in updates and updates[field] is not None and not isinstance(updates[field], str):
                raise ValueError("Invalid mailbox sync value")
        if "status" in updates and (not isinstance(updates["status"], str) or not updates["status"]):
            raise ValueError("Invalid mailbox status")
        if isinstance(updates.get("last_synced_at"), datetime):
            updates["last_synced_at"] = _utc(updates["last_synced_at"]).isoformat()
        if "last_synced_at" in updates and updates["last_synced_at"] is not None:
            if not isinstance(updates["last_synced_at"], str):
                raise ValueError("Invalid mailbox sync timestamp")
            try:
                updates["last_synced_at"] = _utc(datetime.fromisoformat(updates["last_synced_at"])).isoformat()
            except ValueError:
                raise ValueError("Invalid mailbox sync timestamp") from None
        with self._connection(write=True) as connection:
            account = self._account(connection, account_id)
            if not self._generation_matches(account, expected_generation):
                return False
            account.update(updates)
            self._put_account(connection, account)
            return True

    def disconnect_account(self, account_id: str) -> dict:
        with self._connection(write=True) as connection:
            account = self._require_account(connection, account_id)
            if account["provider"] != "gmail":
                raise ValueError("The simulated mailbox cannot be disconnected")
            account.update({"connected": False, "status": "disconnected", "error": None,
                            "generation": uuid.uuid4().hex})
            self._put_account(connection, account)
            connection.execute("UPDATE accounts SET token_blob=NULL WHERE id=?", (account_id,))
            return account_public(account)

    def get_active_account_id(self) -> str:
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM state WHERE key='active_account_id'").fetchone()
            return row[0] if row and self._account(connection, row[0]) is not None else "demo"

    def set_active_account_id(self, account_id: str) -> None:
        with self._connection(write=True) as connection:
            self._require_account(connection, account_id)
            connection.execute("INSERT OR REPLACE INTO state (key,value) VALUES ('active_account_id',?)", (account_id,))

    @staticmethod
    def _preferences(connection: sqlite3.Connection, account_id: str) -> dict:
        row = connection.execute("SELECT payload FROM preferences WHERE account_id=?", (account_id,)).fetchone()
        return {**_PREFERENCE_DEFAULTS, **(json.loads(row[0]) if row else {})}

    def get_preferences(self, account_id: str = "demo") -> dict:
        with self._connection() as connection:
            self._require_account(connection, account_id)
            return self._preferences(connection, account_id)

    def set_preferences(self, account_id: str, changes: dict) -> dict:
        if not isinstance(changes, dict) or set(changes) - set(_PREFERENCE_DEFAULTS):
            raise ValueError("Unknown inbox preference")
        if any(not isinstance(value, str) for value in changes.values()):
            raise ValueError("Inbox preferences must be strings")
        choices = {
            "view": {"inbox", "unread", "starred", "archived"}, "mode": {"priority", "all"},
            "category": {"", *(category["id"] for category in CATEGORIES)},
            "priority": {"", "important", *PRIORITIES},
        }
        for field, values in choices.items():
            if field in changes and changes[field] not in values:
                raise ValueError("Invalid inbox preference")
        if len(changes.get("search", "")) > 500:
            raise ValueError("Inbox search must be 500 characters or fewer")
        with self._connection(write=True) as connection:
            self._require_account(connection, account_id)
            preferences = {**self._preferences(connection, account_id), **changes}
            connection.execute("INSERT OR REPLACE INTO preferences (account_id,payload) VALUES (?,?)",
                               (account_id, json.dumps(preferences, ensure_ascii=False)))
            return preferences

    def upsert_remote_message(self, account_id: str, remote: dict, *, expected_generation: str) -> dict | None:
        remote_id = remote.get("id") if isinstance(remote, dict) else None
        if not isinstance(remote_id, str) or not remote_id:
            raise ValueError("Invalid Gmail message ID")
        if not isinstance(remote.get("raw"), bytes) or not isinstance(remote.get("internal_date"), datetime):
            raise ValueError("Invalid Gmail message content")
        labels = remote.get("labels")
        if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
            raise ValueError("Invalid Gmail message labels")
        if not isinstance(remote.get("thread_id"), str):
            raise ValueError("Invalid Gmail thread ID")
        message_id = f"{account_id}-message-" + hashlib.sha256(remote_id.encode("utf-8")).hexdigest()
        with self._connection(write=True) as connection:
            account = self._account(connection, account_id)
            if not self._generation_matches(account, expected_generation) or account["provider"] != "gmail":
                return None
            row = connection.execute("SELECT payload FROM messages WHERE id=?", (message_id,)).fetchone()
            inserted = row is None
            if inserted:
                message = self._message(remote["raw"], message_id, "gmail", _utc(remote["internal_date"]))
            else:
                # Existing body, application labels, decisions and read history
                # are snapshots. Provider updates affect only provider metadata
                # and fields that the user has never explicitly changed.
                message = json.loads(row[0])
            provider_flags = {"unread": "UNREAD" in labels, "starred": "STARRED" in labels,
                              "archived": "INBOX" not in labels}
            for flag, value in provider_flags.items():
                if not message.get("local_flags", {}).get(flag):
                    message[flag] = value
            message.update({"account_id": account_id, "remote_id": remote_id,
                            "thread_id": remote["thread_id"], "provider_labels": list(labels),
                            "remote_deleted": False, "received_at": _utc(remote["internal_date"]).isoformat()})
            self._put(connection, message)
            return {"message": message, "inserted": inserted}

    def mark_remote_deleted(self, account_id: str, remote_id: str, *, expected_generation: str) -> None:
        message_id = f"{account_id}-message-" + hashlib.sha256(remote_id.encode("utf-8")).hexdigest()
        with self._connection(write=True) as connection:
            if not self._generation_matches(self._account(connection, account_id), expected_generation):
                return
            row = connection.execute("SELECT payload FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is not None:
                message = json.loads(row[0])
                message["remote_deleted"] = True
                if not message.get("local_flags", {}).get("archived"):
                    message["archived"] = True
                self._put(connection, message)

    def save_auth_state(self, state_hash: str, browser_hash: str, payload: str, expires_at: float) -> None:
        if any(not isinstance(value, str) or not value for value in (state_hash, browser_hash, payload)):
            raise ValueError("Invalid authorization transaction")
        if type(expires_at) not in {float, int} or not math.isfinite(expires_at):
            raise ValueError("Invalid authorization expiry")
        with self._connection(write=True) as connection:
            connection.execute("DELETE FROM auth_states WHERE expires_at<=?", (time.time(),))
            connection.execute("INSERT OR REPLACE INTO auth_states (state_hash,browser_hash,payload,expires_at) VALUES (?,?,?,?)",
                               (state_hash, browser_hash, payload, expires_at))

    def consume_auth_state(self, state_hash: str, browser_hash: str) -> dict | None:
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT payload,expires_at FROM auth_states WHERE state_hash=? AND browser_hash=?", (state_hash, browser_hash),
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM auth_states WHERE state_hash=?", (state_hash,))
            return {"payload": row[0], "expires_at": row[1]} if row[1] > time.time() else None

    @staticmethod
    def _classification_matches(annotation: dict, model: str, rubric: str) -> bool:
        # The API may resolve the jev-latest alias to a concrete response model.
        requested = annotation.get("requested_model", annotation.get("model"))
        return requested == model and annotation.get("rubric_version") == rubric

    def classification_queue(self, model: str, rubric: str, *, force: bool = False,
                             retry: bool = False, recover: bool = False) -> None:
        """Persist eligible jobs, keeping failed work paused until explicit retry."""
        with self._connection(write=True) as connection:
            for message in self._rows(connection):
                if message.get("overridden"):
                    connection.execute("DELETE FROM classification_jobs WHERE message_id = ?", (message["id"],))
                    continue
                annotation = self._annotation(message)
                matches = self._classification_matches(annotation, model, rubric)
                state = annotation.get("status")
                if not force and matches:
                    if state == "complete" and annotation.get("provider") == "jev":
                        continue
                    if state == "failed" and not retry:
                        continue
                    if state in {"pending", "processing"} and not recover:
                        job = connection.execute(
                            "SELECT 1 FROM classification_jobs WHERE message_id = ?", (message["id"],)
                        ).fetchone()
                        if job:
                            continue
                message["classification"] = {
                    **annotation, "status": "pending", "requested_model": model,
                    "rubric_version": rubric,
                }
                message["classification"].pop("error", None)
                self._put(connection, message)
                connection.execute(
                    "INSERT OR REPLACE INTO classification_jobs (message_id,token,state,model,rubric) "
                    "VALUES (?,?,?,?,?)", (message["id"], uuid.uuid4().hex, "pending", model, rubric),
                )

    def classification_claim(self, model: str, rubric: str) -> dict | None:
        """Claim one immutable snapshot in a short transaction, with no I/O."""
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT j.message_id,j.token,m.payload FROM classification_jobs j "
                "JOIN messages m ON m.id=j.message_id "
                "WHERE j.state='pending' AND j.model=? AND j.rubric=? ORDER BY j.rowid LIMIT 1",
                (model, rubric),
            ).fetchone()
            if row is None:
                return None
            message = json.loads(row[2])
            if message.get("overridden"):
                connection.execute("DELETE FROM classification_jobs WHERE message_id=?", (row[0],))
                return None
            message["classification"] = {**self._annotation(message), "status": "processing"}
            self._put(connection, message)
            connection.execute("UPDATE classification_jobs SET state='processing' WHERE message_id=?", (row[0],))
            return {"message": message, "token": row[1]}

    def classification_finish(self, message_id: str, token: str, *, result: dict | None = None,
                              error: str | None = None, model: str, rubric: str) -> bool:
        """Merge only AI annotations, if this exact job still owns the record."""
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT m.payload FROM messages m JOIN classification_jobs j ON j.message_id=m.id "
                "WHERE m.id=? AND j.token=? AND j.state='processing'", (message_id, token),
            ).fetchone()
            if row is None:
                return False
            message = json.loads(row[0])
            if message.get("overridden"):
                return False
            if error is not None:
                message["classification"] = {**self._annotation(message), "status": "failed", "error": error}
                connection.execute("UPDATE classification_jobs SET state='failed' WHERE message_id=?", (message_id,))
            else:
                for field in ("category", "priority", "score", "labels", "reasons"):
                    message[field] = result[field]
                message["classification"] = {
                    **result["classification"], "requested_model": model, "rubric_version": rubric,
                }
                connection.execute("DELETE FROM classification_jobs WHERE message_id=?", (message_id,))
            self._put(connection, message)
            return True

    def classification_counts(self, model: str | None, rubric: str, account_id: str | None = None) -> dict:
        """Include archived messages in progress and keep counts mutually exclusive."""
        with self._connection() as connection:
            messages = self._rows(connection)
            if account_id is not None:
                self._require_account(connection, account_id)
                messages = [message for message in messages if message.get("account_id", "demo") == account_id]
        counts = {"total": len(messages), "complete": 0, "pending": 0, "failed": 0, "manual": 0, "error": None}
        for message in messages:
            annotation = self._annotation(message)
            if message.get("overridden"):
                counts["manual"] += 1
            elif annotation.get("status") == "failed":
                counts["failed"] += 1
                counts["error"] = counts["error"] or annotation.get("error")
            elif (annotation.get("provider") == "jev" and annotation.get("status") == "complete"
                  and (model is None or self._classification_matches(annotation, model, rubric))):
                counts["complete"] += 1
            else:
                counts["pending"] += 1
        return counts
