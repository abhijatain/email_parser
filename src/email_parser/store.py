"""Persistent local inbox with a provider-independent ingestion pipeline."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from .demo import BATCH_COUNT, messages_for_batch
from .parser import parse_email
from .triage import CATEGORIES, PRIORITIES, classify_email


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
        self._lock = threading.RLock()
        with self._connection(write=True) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, source TEXT NOT NULL, payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
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
                if "classification" not in message:
                    message["classification"] = self._annotation(message)
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
                      view: str = "inbox") -> list[dict]:
        if any(not isinstance(value, str) for value in (search, category, priority, view)):
            raise ValueError("Inbox filters must be strings")
        if category and category not in {item["id"] for item in CATEGORIES}:
            raise ValueError("Unknown category")
        if priority and priority not in PRIORITIES:
            raise ValueError("Unknown priority")
        if view not in {"inbox", "unread", "starred", "archived"}:
            raise ValueError("Unknown inbox view")
        with self._connection() as connection:
            messages = self._rows(connection)
        query = search.casefold().strip()
        result = []
        for message in messages:
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

    def get_message(self, message_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT payload FROM messages WHERE id = ?", (message_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def update_message(self, message_id: str, changes: dict) -> dict | None:
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
            message.update(changes)
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

    def summary(self) -> dict:
        with self._connection() as connection:
            messages = self._rows(connection)
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
                "demo_batches_remaining": max(0, BATCH_COUNT - batch)}

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
            connection.execute("DELETE FROM classification_jobs WHERE message_id IN (SELECT id FROM messages WHERE source = 'demo')")
            connection.execute("DELETE FROM messages WHERE source = 'demo'")
            self._seed(connection, _utc_now())

    def import_email(self, data: bytes) -> dict:
        if not isinstance(data, bytes):
            raise ValueError("Email input must be bytes")
        message_id = "import-" + hashlib.sha256(data).hexdigest()
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT payload FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row:
                return json.loads(row[0])
            message = self._message(data, message_id, "import", _utc_now())
            self._put(connection, message)
            return message

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

    def classification_counts(self, model: str | None, rubric: str) -> dict:
        """Include archived messages in progress and keep counts mutually exclusive."""
        with self._connection() as connection:
            messages = self._rows(connection)
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
