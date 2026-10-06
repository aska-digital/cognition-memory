"""Fetch-first merge with keep-both conflicts for cognition memory.

Push never blindly overwrites: it fetches the committed state for the entry's
ref first and compares it against the base the writer saw. When the base still
matches, the push applies. When something else committed in between, both
versions are kept — the existing entry stays under its ref and the incoming
one lands under a deterministic conflict ref
``{ref}-conflict-{unix_ts}`` — so no write ever loses another writer's facts.

Shared-store transaction: the on-disk ``committed.json`` is the sole source
of truth — in-memory state is a cache, never authority. Every push and remove
runs refresh → compare → write as ONE transaction under a cross-instance /
cross-process file exclusion spanning all three steps, so two ``CommitLog``
instances (same process or different processes) on one path cannot interleave
a stale read with another writer's commit. The per-ref single-writer lock
nests INSIDE that exclusion, and the instance lock is innermost; this fixed
outermost → innermost order (store exclusion → per-ref lock → instance lock)
is the only order this module ever acquires them in. Same-instance keep-both
semantics are unchanged: a stale base still lands under a conflict ref.

Concurrency: one single-writer lock per entry id serializes concurrent pushes
to the same ref. Dream (read-only synthesis) sees committed state only: it
reads through :func:`committed_snapshot`, which can never observe the push
queue or in-flight merges.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:  # POSIX file exclusion for the cross-process transaction
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

try:  # Windows file exclusion (best effort; POSIX path is authoritative)
    import msvcrt
except ImportError:  # pragma: no cover - POSIX hosts have no msvcrt
    msvcrt = None  # type: ignore[assignment]

from .provenance import validate_provenance


def conflict_ref(ref: str, now: Optional[float] = None) -> str:
    """Deterministic conflict ref: ``{ref}-conflict-{unix_ts}``."""
    return "%s-conflict-%d" % (ref, int(now if now is not None else time.time()))


class EntryWriteLock:
    """Single-writer lock per entry id (re-entrant per thread)."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.RLock] = {}

    @contextmanager
    def hold(self, entry_id: str) -> Iterator[None]:
        """Hold the exclusive write lock for ``entry_id``."""
        with self._guard:
            lock = self._locks.get(entry_id)
            if lock is None:
                lock = self._locks[entry_id] = threading.RLock()
        with lock:
            yield


# In-process half of the store exclusion: keyed by canonical store path so
# two CommitLog instances on one path in one process serialize even where the
# OS lock is unavailable. (Where fcntl/msvcrt exists the OS lock additionally
# excludes separate processes; threads also serialize on the guard first.)
_FILE_GUARD = threading.Lock()
_FILE_GUARDS: Dict[str, threading.RLock] = {}


def _guard_for(path: str) -> threading.RLock:
    key = os.path.abspath(path)
    with _FILE_GUARD:
        guard = _FILE_GUARDS.get(key)
        if guard is None:
            guard = _FILE_GUARDS[key] = threading.RLock()
        return guard


@contextmanager
def _store_exclusion(path: str) -> Iterator[None]:
    """Hold cross-instance/process exclusion for the store at ``path``.

    Outermost lock of the shared-store transaction: every refresh →
    compare → write sequence runs inside this. The per-ref
    :class:`EntryWriteLock` nests inside it, never the reverse.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    guard = _guard_for(path)
    with guard:
        lock_path = path + ".lock"
        with open(lock_path, "a+b") as handle:
            fileno = handle.fileno()
            if fcntl is not None:
                fcntl.flock(fileno, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fileno, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows best effort
                handle.seek(0)
                msvcrt.locking(fileno, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:  # pragma: no cover - no OS lock available; guard only
                yield


class CommitLog:
    """File-backed committed state: ref -> entry (atomic JSON persistence).

    The disk file is the sole truth; ``self._entries`` is a cache refreshed
    from disk inside every locked operation.
    """

    def __init__(self, path: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = self._read_disk()

    def _read_disk(self) -> Dict[str, Dict[str, Any]]:
        """Current disk truth (missing/blank file reads as empty)."""
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = handle.read().strip()
        except FileNotFoundError:
            return {}
        if not raw:
            return {}
        return json.loads(raw)

    def _reload_locked(self) -> None:
        """Refresh the cache from disk truth (instance lock held)."""
        self._entries = self._read_disk()

    def _persist_locked(self, entries: Dict[str, Dict[str, Any]]) -> None:
        """Publish the candidate snapshot only after its replacement succeeds."""
        directory = os.path.dirname(self._path)
        fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=".commitlog-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, self._path)
            self._entries = entries
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, ref: str) -> Optional[Dict[str, Any]]:
        """Committed entry for ``ref`` (deep copy; None when absent)."""
        with _store_exclusion(self._path):
            with self._lock:
                self._reload_locked()
                entry = self._entries.get(ref)
                return copy.deepcopy(entry) if entry is not None else None

    def refs(self) -> List[str]:
        """All committed refs."""
        with _store_exclusion(self._path):
            with self._lock:
                self._reload_locked()
                return sorted(self._entries)

    def entries_for_repo(self, origin_repo: str) -> List[Dict[str, Any]]:
        """Committed entries routed to one internal repo (multi-repo routing)."""
        with _store_exclusion(self._path):
            with self._lock:
                self._reload_locked()
                return [
                    copy.deepcopy(entry)
                    for entry in self._entries.values()
                    if isinstance(entry.get("provenance"), dict)
                    and entry["provenance"].get("origin_repo") == origin_repo
                ]

    def _apply_locked(self, entry: Dict[str, Any]) -> None:
        validate_provenance(entry)
        entries = dict(self._entries)
        entries[entry["ref"]] = copy.deepcopy(entry)
        self._persist_locked(entries)

    def remove(self, ref: str) -> Optional[Dict[str, Any]]:
        """Remove a committed entry; returns the removed copy (None if absent)."""
        with _store_exclusion(self._path):
            with _WRITE_LOCKS.hold(ref):
                with self._lock:
                    self._reload_locked()
                    entry = self._entries.get(ref)
                    if entry is not None:
                        entries = dict(self._entries)
                        del entries[ref]
                        self._persist_locked(entries)
                    return copy.deepcopy(entry) if entry is not None else None


# One process-wide lock registry: concurrent pushes to the same ref from any
# CommitLog instance serialize on the entry id, not the instance.
_WRITE_LOCKS = EntryWriteLock()


def fetch_first_push(
    log: CommitLog,
    entry: Dict[str, Any],
    base: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> Tuple[str, str]:
    """Fetch-first push of ``entry`` against ``log``.

    ``base`` is the committed state the writer saw when it built the entry
    (None for a brand-new ref). Returns ``(status, ref)`` where status is
    ``"applied"`` (fast path: base still matches committed) or ``"conflict"``
    (keep-both: existing entry untouched, incoming stored under a conflict
    ref). The returned ref is where the incoming content landed.

    Runs refresh → compare → write as ONE transaction: disk is re-read under
    the store exclusion, compared, and written before the exclusion is
    released, with the per-ref lock nested inside.
    """
    validate_provenance(entry)
    ref = entry.get("ref")
    if not ref:
        raise ValueError("entry has no ref")
    with _store_exclusion(log._path):
        with _WRITE_LOCKS.hold(ref):
            with log._lock:
                log._reload_locked()
                committed = copy.deepcopy(log._entries.get(ref))
                if committed is None or committed == base:
                    log._apply_locked(entry)
                    return ("applied", ref)
                incoming = copy.deepcopy(entry)
                target = conflict_ref(ref, now)
                suffix = 2
                while target in log._entries:  # same-second collision: stay unique
                    target = "%s-%d" % (conflict_ref(ref, now), suffix)
                    suffix += 1
                incoming["ref"] = target
                incoming["conflict_with"] = ref
                log._apply_locked(incoming)
                return ("conflict", incoming["ref"])


def committed_snapshot(log: CommitLog) -> List[Dict[str, Any]]:
    """Deep copy of all committed entries — the only state Dream may read."""
    with _store_exclusion(log._path):
        with log._lock:
            log._reload_locked()
            return copy.deepcopy(list(log._entries.values()))
