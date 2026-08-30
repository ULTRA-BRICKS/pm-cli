"""The append-only JSONL event store — the durable spine of the PM queue.

WHAT THIS FILE IS
    ``EventStore`` (append one event, read them all back in order) + ``resolve_store_dir`` (where
    the log lives). Stdlib only.

HOW IT'S REACHED
    The CLI resolves the store dir, constructs an ``EventStore``, appends observation events, and
    replays ``read_all()`` through ``projection.build_projection`` to render the queue.

INVARIANTS
    - **Append-only + fsync'd.** Each ``append`` opens in ``a`` mode, writes one line, and
      ``flush()`` + ``os.fsync`` so a crash can't lose an acknowledged event. Ordering is file
      order (== append order == the total ISO-timestamp order).
    - **Torn-line tolerant on read.** A crash mid-write can leave a partial last line; a bad or
      blank line is SKIPPED, never fatal (mirrors task-cli: one bad record can't crash the queue).
    - **Store dir is resolved once from ``$PM_HOME`` or an XDG-state default** so tests point it
      at ``tmp_path`` and never touch the real HOME.
    - **``lock()`` serializes a read-validate-append sequence — hold it, or the sequence is
      racy.** A caller that reads current state, validates a command against it, and only THEN
      appends must hold ``lock()`` across all three steps, or two concurrent processes can both
      pass validation against the same stale read and each append an event the other's decision
      already invalidated (e.g. two processes racing ``sprint add`` against a ``sprint start``).
      ``append``/``read_all`` alone are individually safe (single fsync'd write, tolerant read)
      but do NOT protect a multi-step decision. NOTE: as of this writing not every existing
      validating writer in this codebase has been migrated to hold this lock yet (``sprint``'s
      commands do; ``commands/record.py``'s transition path does not — tracked separately) — new
      and touched read-validate-append code should adopt it, but its mere existence doesn't imply
      universal coverage.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .events import Event

try:  # POSIX (macOS/Linux) -- the primary target
    import fcntl

    def _lock_exclusive(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
except ImportError:  # Windows has no fcntl -- fall back to msvcrt's byte-range lock
    import time

    import msvcrt

    def _lock_exclusive(fh) -> None:
        # msvcrt locks a byte range starting at the current position, not the whole file;
        # `lock()` always opens in "a+" (append) mode, so seek to a fixed offset first.
        # `LK_LOCK` itself only retries for ~10s before raising OSError -- that violates this
        # module's "blocks until acquired" contract on its own, so wrap it in an outer retry
        # loop that keeps trying indefinitely, the same guarantee flock(LOCK_EX) gives for free.
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


_LOG_NAME = "events.jsonl"
_LOCK_NAME = "events.lock"


def resolve_store_dir() -> Path:
    """The directory holding the event log. ``$PM_HOME`` wins; else ``$XDG_STATE_HOME/pm``."""
    env = os.environ.get("PM_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "pm"


class EventStore:
    """Append-only JSONL event log at ``<dir>/events.jsonl``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.log_path = directory / _LOG_NAME
        self.lock_path = directory / _LOCK_NAME

    @contextlib.contextmanager
    def lock(self):
        """Hold an exclusive advisory lock across a read-validate-append sequence.

        Blocks until acquired (no timeout — a caller genuinely waiting on another process's
        in-flight command is correct; there is no scenario where giving up mid-wait is safer than
        blocking). Released automatically on exit, including on an exception.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as fh:
            _lock_exclusive(fh)
            try:
                yield
            finally:
                _unlock(fh)

    def append(self, event: Event) -> Event:
        """Durably append one event (create the dir on first write). Returns it for chaining."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(event.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def read_all(self) -> list[Event]:
        """Every event in append order. Blank/torn lines are skipped, never fatal."""
        if not self.log_path.exists():
            return []
        events: list[Event] = []
        with self.log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(Event.from_json(line))
                except (ValueError, KeyError, TypeError):
                    # ValueError/KeyError: bad JSON or a missing required field (torn/partial
                    # write). TypeError: valid JSON but the wrong SHAPE for a field `from_json`
                    # coerces without checking first, e.g. a scalar `"payload":1` -- `dict(1)`
                    # raises TypeError rather than a JSON-decode error, but it's the same class
                    # of malformed/foreign line and must be equally non-fatal to the whole read.
                    continue
        return events
