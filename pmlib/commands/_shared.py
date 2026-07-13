"""Shared plumbing for the command handlers — open the store, project the queue.

Underscore-prefixed so command discovery skips it (it exposes no ``NAME``/``run``).
"""

from __future__ import annotations

from ..model import WorkItem
from ..projection import build_projection
from ..states import StateMachine, default_machine
from ..store import EventStore, resolve_store_dir


def open_store() -> EventStore:
    """The event store rooted at the resolved store dir (``$PM_HOME`` or the XDG default)."""
    return EventStore(resolve_store_dir())


def machine() -> StateMachine:
    """The active state machine (the default registry for Release 1)."""
    return default_machine()


def load_items(store: EventStore, m: StateMachine | None = None) -> dict[str, WorkItem]:
    """Replay the log into the current work-item projection (reusing ``m`` if the caller has one)."""
    return build_projection(store.read_all(), m or machine())
