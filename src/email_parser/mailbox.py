"""Durable, read-only Gmail synchronization outside database transactions.

The Gmail history cursor is committed only when a complete import succeeds.
Every message write also carries the account generation so a disconnected or
reconnected account cannot receive results from an older in-flight request.
"""

from __future__ import annotations

import errno
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .gmail import GmailAuthError, GmailClient, GmailError, GmailHistoryExpired, GmailMessageMissing


_EXCLUDED_LABELS = {"SENT", "DRAFT", "SPAM", "TRASH"}
_FAILURE = "Gmail synchronization failed. Try syncing again."
_OWNERSHIP = (
    "Another inbox server is already synchronizing Gmail for this database. "
    "Stop that server before starting another."
)


class _MailboxOwnership:
    """Keep a stable lock inode; the operating system releases it on exit."""

    def __init__(self, database: Path):
        self._file = Path(str(database.resolve()) + ".mailbox.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self._file.seek(0, os.SEEK_END) == 0:
                    self._file.write(b"\0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._file.close()
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ValueError(_OWNERSHIP) from error
            raise OSError("Cannot lock the Gmail worker database. Use a writable local database path.") from error
        except BaseException:
            self._file.close()
            raise

    def close(self):
        self._file.close()


class _Cancelled(Exception):
    """The account generation or worker lifetime changed during a request."""


def _datetime(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Expected a timezone-aware Gmail timestamp")
    return value.astimezone(timezone.utc)


def _cursor(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Gmail did not provide a history cursor")
    return value


def _safe_error(error: Exception) -> str:
    # Adapter errors deliberately exclude transport bodies, tokens and URLs.
    return str(error) if isinstance(error, GmailError) else _FAILURE


class MailboxService:
    """One bounded background poller for all connected mailboxes in a database."""

    def __init__(self, store, auth, *, client_factory=GmailClient, on_messages=None,
                 interval=60, enabled=True):
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
            raise ValueError("The Gmail polling interval must be positive and finite.")
        self.store = store
        self.auth = auth
        self.client_factory = client_factory
        self.on_messages = on_messages
        self.interval = float(interval)
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._queue = deque()
        self._queued: set[str] = set()
        self._states: dict[str, dict] = {}
        self._next_due: dict[str, float] = {}
        self._active: tuple[str, str] | None = None
        self._ownership: _MailboxOwnership | None = None
        self._thread: threading.Thread | None = None
        if self.enabled:
            try:
                self._ownership = _MailboxOwnership(Path(self.store.db_path))
                self._thread = threading.Thread(target=self._run, name="gmail-inbox-sync", daemon=True)
                self._thread.start()
            except BaseException:
                self._stopped.set()
                self._release_ownership()
                raise

    @staticmethod
    def _empty(account_id, state="idle", error=None):
        return {"state": state, "account_id": account_id, "processed": 0, "added": 0, "error": error}

    def status(self, account_id=None) -> dict:
        if account_id is None:
            account_id = self.store.get_active_account_id()
        account = self.store.get_account(account_id)
        if account is None:
            return self._empty(account_id, "disconnected")
        if account.get("provider") != "gmail" or not account.get("connected"):
            return self._empty(account_id, "disconnected")
        with self._lock:
            current = self._states.get(account_id)
            if current and current.get("_generation") == account.get("generation"):
                return {key: value for key, value in current.items() if not key.startswith("_")}
        error = account.get("error")
        return self._empty(account_id, "error" if error else "idle", error)

    def enqueue(self, account_id) -> dict:
        account = self.store.get_account(account_id)
        if account is None or account.get("provider") != "gmail":
            raise ValueError("Choose a Gmail account to synchronize.")
        if not account.get("connected") or not self.enabled or self._stopped.is_set():
            return self.status(account_id)
        generation = account["generation"]
        with self._lock:
            if account_id in self._queued or self._active == (account_id, generation):
                return self.status(account_id)
            if not self.store.update_account_sync(
                account_id, {"status": "queued", "error": None}, expected_generation=generation,
            ):
                return self.status(account_id)
            self._states[account_id] = {**self._empty(account_id, "queued"), "_generation": generation}
            self._queue.append(account_id)
            self._queued.add(account_id)
            self._next_due[account_id] = time.monotonic() + self.interval
            self._wake.set()
        return self.status(account_id)

    def _schedule_due(self):
        now = time.monotonic()
        for public in self.store.list_accounts():
            if public.get("provider") != "gmail" or not public.get("connected") or public.get("status") == "needs_reauth":
                continue
            account_id = public["id"]
            with self._lock:
                due = self._next_due.get(account_id, 0) <= now
                busy = account_id in self._queued or (self._active is not None and self._active[0] == account_id)
            if due and not busy:
                try:
                    self.enqueue(account_id)
                except ValueError:
                    # A disconnect can race this public snapshot.
                    pass

    def _run(self):
        try:
            while not self._stopped.is_set():
                self._schedule_due()
                with self._lock:
                    self._wake.clear()
                    account_id = self._queue.popleft() if self._queue else None
                    if account_id is not None:
                        self._queued.discard(account_id)
                if account_id is None:
                    # close() may have set the event while _schedule_due was
                    # running, just before the clear above. Check the durable
                    # stop flag before waiting so shutdown cannot lose its wake
                    # and return while this worker still owns the mailbox lock.
                    if self._stopped.is_set():
                        break
                    # A bounded wait also discovers connections made by other
                    # request handlers, without spinning on a paused account.
                    self._wake.wait(min(self.interval, 1.0))
                    continue
                account = self.store.get_account(account_id)
                if not account or not account.get("connected"):
                    continue
                generation = account["generation"]
                with self._lock:
                    self._active = (account_id, generation)
                    self._states[account_id] = {**self._empty(account_id, "syncing"), "_generation": generation}
                try:
                    self._synchronize(account)
                except _Cancelled:
                    pass
                except Exception as error:
                    if self._stopped.is_set():
                        continue
                    message = _safe_error(error)
                    if self.store.update_account_sync(account_id, {
                        "status": "needs_reauth" if isinstance(error, GmailAuthError) else "error",
                        "error": message,
                    }, expected_generation=generation):
                        self._set_state(account_id, generation, state="error", error=message)
                finally:
                    with self._lock:
                        self._active = None
                        self._next_due[account_id] = time.monotonic() + self.interval
        finally:
            self._release_ownership()

    def _assert_current(self, account_id, generation):
        if self._stopped.is_set():
            raise _Cancelled()
        current = self.store.get_account(account_id)
        if not current or not current.get("connected") or current.get("generation") != generation:
            raise _Cancelled()

    def _set_state(self, account_id, generation, **changes):
        with self._lock:
            state = self._states.get(account_id)
            if state is not None and state.get("_generation") == generation:
                state.update(changes)

    def _progress(self, account_id, generation, *, inserted=False):
        with self._lock:
            state = self._states.get(account_id)
            if state is not None and state.get("_generation") == generation:
                state["processed"] += 1
                state["added"] += bool(inserted)

    def _notify(self, account_id, generation):
        self._assert_current(account_id, generation)
        if self.on_messages is not None:
            try:
                self.on_messages()
            except Exception:
                # Imported rows are durable and the classifier recovers pending
                # rows on its own startup. A notification is not a Gmail error.
                pass

    def _synchronize(self, account):
        account_id, generation = account["id"], account["generation"]
        self._assert_current(account_id, generation)
        if not self.store.update_account_sync(account_id, {"status": "syncing", "error": None}, expected_generation=generation):
            raise _Cancelled()
        token = self.auth.access_token(account_id)
        self._assert_current(account_id, generation)
        client = self.client_factory(token)
        started_at = datetime.now(timezone.utc)
        covered_until = started_at
        try:
            if not account.get("initial_import_complete"):
                anchor = account.get("initial_history_id")
                if not anchor:
                    anchor = _cursor(client.profile().get("historyId"))
                    self._assert_current(account_id, generation)
                    if not self.store.update_account_sync(account_id, {"initial_history_id": anchor}, expected_generation=generation):
                        raise _Cancelled()
                self._scan(client, account, _datetime(account["initial_since"]), _datetime(account["initial_before"]))
                cursor = _cursor(anchor)
                # This scan covers only the frozen connection-time window.
                # A delayed backfill has not caught up to the worker's start
                # time yet. If its old history anchor expires, recovery must
                # resume from this boundary rather than skip the elapsed days.
                covered_until = _datetime(account["initial_before"])
            else:
                try:
                    cursor = self._history(client, account, _cursor(account.get("history_id")))
                except GmailHistoryExpired:
                    # Capture an anchor first: arrivals while paginating this
                    # recovery scan are picked up by the next history request.
                    cursor = _cursor(client.profile().get("historyId"))
                    after = _datetime(account.get("last_synced_at") or account["initial_since"])
                    if account.get("last_synced_at"):
                        after -= timedelta(minutes=5)
                    self._scan(client, account, after, None)
            self._assert_current(account_id, generation)
            if not self.store.update_account_sync(account_id, {
                "history_id": cursor, "initial_import_complete": True,
                "last_synced_at": covered_until.isoformat(), "status": "connected", "error": None,
            }, expected_generation=generation):
                raise _Cancelled()
            self._set_state(account_id, generation, state="idle", error=None)
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def _fetch(self, client, account, remote_id, *, after=None, before=None):
        account_id, generation = account["id"], account["generation"]
        self._assert_current(account_id, generation)
        try:
            remote = client.get_message(remote_id)
        except GmailMessageMissing:
            self._assert_current(account_id, generation)
            self.store.mark_remote_deleted(account_id, remote_id, expected_generation=generation)
            self._progress(account_id, generation)
            return False
        self._assert_current(account_id, generation)
        labels = set(remote["labels"])
        if labels & _EXCLUDED_LABELS:
            if after is None:
                self.store.mark_remote_deleted(account_id, remote_id, expected_generation=generation)
            self._progress(account_id, generation)
            return False
        internal_date = _datetime(remote["internal_date"])
        if (after is not None and internal_date < after) or (before is not None and internal_date > before):
            self._progress(account_id, generation)
            return False
        result = self.store.upsert_remote_message(account_id, remote, expected_generation=generation)
        if result is None:
            raise _Cancelled()
        self._progress(account_id, generation, inserted=result["inserted"])
        return result["inserted"]

    @staticmethod
    def _next_page(response, seen):
        token = response.get("nextPageToken")
        if token is not None and (not isinstance(token, str) or not token or token in seen):
            raise ValueError("Gmail returned an invalid pagination cursor")
        if token is not None:
            seen.add(token)
        return token

    def _scan(self, client, account, after, before):
        account_id, generation = account["id"], account["generation"]
        page_token, seen = None, set()
        while True:
            self._assert_current(account_id, generation)
            page = client.list_messages(after=after, before=before, page_token=page_token)
            self._assert_current(account_id, generation)
            added = False
            try:
                for item in page.get("messages", []):
                    added |= self._fetch(client, account, item["id"], after=after, before=before)
            finally:
                # Earlier inserts already committed even if a later download
                # failed. Retrying can only see duplicates or deleted messages,
                # so those durable rows must be queued for classification now.
                if added:
                    self._notify(account_id, generation)
            page_token = self._next_page(page, seen)
            if page_token is None:
                return

    def _history(self, client, account, history_id):
        account_id, generation = account["id"], account["generation"]
        page_token, seen = None, set()
        changed, deleted = {}, set()
        cursor = history_id
        while True:
            self._assert_current(account_id, generation)
            page = client.history(history_id, page_token=page_token)
            self._assert_current(account_id, generation)
            cursor = _cursor(page.get("historyId"))
            for event in page.get("history", []):
                for kind in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                    for entry in event.get(kind, []):
                        changed[entry["message"]["id"]] = None
                for entry in event.get("messagesDeleted", []):
                    deleted.add(entry["message"]["id"])
            page_token = self._next_page(page, seen)
            if page_token is None:
                break
        added = False
        try:
            for remote_id in changed:
                if remote_id not in deleted:
                    added |= self._fetch(client, account, remote_id)
            for remote_id in deleted:
                self._assert_current(account_id, generation)
                self.store.mark_remote_deleted(account_id, remote_id, expected_generation=generation)
                self._progress(account_id, generation)
        finally:
            if added:
                self._notify(account_id, generation)
        return cursor

    def _release_ownership(self):
        with self._lock:
            ownership, self._ownership = self._ownership, None
        if ownership is not None:
            ownership.close()

    def close(self):
        self._stopped.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # Network calls have bounded adapter timeouts; an in-flight worker
            # keeps ownership until it exits and cannot commit after closing.
            thread.join(timeout=1)
        if thread is None or not thread.is_alive():
            self._release_ownership()
