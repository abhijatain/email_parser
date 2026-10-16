"""Background Jev classification with durable progress and explicit retry.

The worker owns no database transaction while it calls the hosted model. A
unique persisted token lets the store reject responses after reset or edits.
"""

from __future__ import annotations

import errno
import math
import os
import threading
from pathlib import Path

from .store import InboxStore
from .triage import CATEGORIES, PRIORITIES


RUBRIC_VERSION = "jev-inbox-v1"
_CONFIG_MESSAGE = "Configure TYPESAFE_API_KEY on the server to use Jev AI."
_FAILURE_MESSAGE = "Jev classification failed. Check the server configuration and retry."
_OWNERSHIP_MESSAGE = (
    "Another inbox server is already running Jev classification for this database. "
    "Stop that server before starting another."
)


class _WorkerOwnership:
    """OS-managed ownership survives stale lock files and ends on process exit.

    Keep the file in place after unlocking: unlinking a locked file would let a
    second process lock a different inode at the same pathname.
    """

    def __init__(self, database: Path):
        path = Path(str(database.resolve()) + ".jev.lock")
        self._file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                # Windows byte-range locking requires an existing byte and a
                # stable seek position. Every contender locks the same byte.
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
                raise ValueError(_OWNERSHIP_MESSAGE) from error
            raise OSError("Cannot lock the Jev worker database. Use a writable local database path.") from error
        except BaseException:
            self._file.close()
            raise

    def close(self) -> None:
        # Closing releases the advisory lock on Unix and the byte lock on
        # Windows. It also releases automatically if the process crashes.
        self._file.close()


def _safe_error(error: Exception) -> str:
    # Only our adapter's deliberately sanitized messages are safe to expose.
    # SDK/transport exception strings can include request data or credentials.
    from .jev import JevError

    return str(error) if isinstance(error, JevError) else _FAILURE_MESSAGE


def _validate_result(result: dict) -> None:
    """Protect stored inbox fields when an injected classifier is used."""
    if not isinstance(result, dict):
        raise ValueError("Invalid classifier result")
    if result.get("category") not in {item["id"] for item in CATEGORIES}:
        raise ValueError("Invalid category")
    if result.get("priority") not in PRIORITIES:
        raise ValueError("Invalid priority")
    score = result.get("score")
    if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError("Invalid score")
    for field in ("labels", "reasons"):
        if not isinstance(result.get(field), list) or not all(isinstance(value, str) for value in result[field]):
            raise ValueError("Invalid annotations")
    annotation = result.get("classification")
    if not isinstance(annotation, dict) or annotation.get("provider") != "jev" or annotation.get("status") != "complete":
        raise ValueError("Invalid classification provenance")


class ClassificationService:
    """One bounded worker per inbox; a failure pauses further paid requests."""

    def __init__(self, store: InboxStore, classifier=None):
        self.store = store
        self.classifier = classifier
        self.model = classifier.model if classifier is not None else None
        self._state_lock = threading.RLock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._paused = False
        self._error: str | None = None
        self._adapter_closed = False
        self._ownership: _WorkerOwnership | None = None
        self._thread: threading.Thread | None = None
        if classifier is not None:
            try:
                # Recovery replaces unfinished job tokens. Only the exclusive
                # owner may recover, enqueue, or call the paid classifier.
                self._ownership = _WorkerOwnership(self.store.db_path)
                self.store.classification_queue(self.model, RUBRIC_VERSION, recover=True)
                counts = self.store.classification_counts(self.model, RUBRIC_VERSION)
                self._paused = bool(counts["failed"])
                self._error = counts["error"] if self._paused else None
                self._thread = threading.Thread(target=self._run, name="jev-inbox-classifier", daemon=True)
                self._thread.start()
                self._wake.set()
            except BaseException:
                self._stopped.set()
                self._wake.set()
                if self._thread is None or not self._thread.is_alive():
                    self._release_ownership()
                # Until the worker starts, the adapter remains caller-owned.
                # Server construction handles its cleanup on failure.
                raise

    def status(self) -> dict:
        counts = self.store.classification_counts(self.model, RUBRIC_VERSION)
        with self._state_lock:
            error = self._error or counts["error"]
            if self.classifier is None:
                state = "needs_key"
            elif self._paused or counts["failed"]:
                state = "error"
            elif counts["pending"]:
                state = "processing"
            else:
                state = "ready"
        return {"provider": "jev", "model": self.model, "configured": self.classifier is not None,
                "status": state, **counts, "error": error}

    def enqueue(self, force: bool = False) -> dict:
        if self.classifier is not None and not self._stopped.is_set():
            with self._state_lock:
                self.store.classification_queue(self.model, RUBRIC_VERSION, force=force)
                if force:
                    self._paused = False
                    self._error = None
                self._wake.set()
        return self.status()

    def retry(self) -> dict:
        if self.classifier is None:
            raise ValueError(_CONFIG_MESSAGE)
        if self._stopped.is_set():
            raise ValueError("The Jev classifier has stopped. Restart the inbox server.")
        with self._state_lock:
            self.store.classification_queue(self.model, RUBRIC_VERSION, retry=True)
            self._paused = False
            self._error = None
            self._wake.set()
        return self.status()

    def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                self._wake.wait()
                self._wake.clear()
                while not self._stopped.is_set():
                    with self._state_lock:
                        if self._paused:
                            break
                        job = self.store.classification_claim(self.model, RUBRIC_VERSION)
                    if job is None:
                        break
                    try:
                        # Deliberately outside both service and store locks.
                        result = self.classifier.classify(job["message"])
                        _validate_result(result)
                    except Exception as exception:
                        if self._stopped.is_set():
                            break
                        error = _safe_error(exception)
                        with self._state_lock:
                            applied = self.store.classification_finish(
                                job["message"]["id"], job["token"], error=error,
                                model=self.model, rubric=RUBRIC_VERSION,
                            )
                            if applied:
                                self._paused = True
                                self._error = error
                        # A stale failed job is harmless and should not pause a
                        # new inbox or a manual decision that canceled it.
                        if applied:
                            break
                        continue
                    if self._stopped.is_set():
                        break
                    self.store.classification_finish(
                        job["message"]["id"], job["token"], result=result,
                        model=self.model, rubric=RUBRIC_VERSION,
                    )
        finally:
            self._close_adapter()
            self._release_ownership()

    def _release_ownership(self) -> None:
        with self._state_lock:
            ownership, self._ownership = self._ownership, None
        if ownership is not None:
            ownership.close()

    def _close_adapter(self) -> None:
        with self._state_lock:
            if self.classifier is None or self._adapter_closed:
                return
            self._adapter_closed = True
        try:
            self.classifier.close()
        except Exception:
            # Closing must not leak transport exception details or hide server
            # shutdown. The adapter no longer receives any new work.
            pass

    def close(self) -> None:
        self._stopped.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # The adapter enforces its network timeout. If a call is in flight,
            # its worker closes the client after it exits, avoiding a race.
            thread.join(timeout=1)
        if thread is None or not thread.is_alive():
            self._close_adapter()
            self._release_ownership()
