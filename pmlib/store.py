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
"""

from __future__ import annotations

import os
from pathlib import Path

from .events import Event

_LOG_NAME = "events.jsonl"


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
                except (ValueError, KeyError):
                    continue  # torn/partial line from a crashed append — skip it
        return events
