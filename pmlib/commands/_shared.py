"""Shared plumbing for the command handlers — open the store, project the queue.

Underscore-prefixed so command discovery skips it (it exposes no ``NAME``/``run``).
"""

from __future__ import annotations

import re

from ..model import WorkItem
from ..projection import build_projection
from ..states import StateMachine, default_machine
from ..store import EventStore, resolve_store_dir

_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-]")


def display_safe(text: str) -> str:
    """Escape control characters (ASCII C0 + DEL, and Unicode C1 U+0080-U+009F) before printing
    free-text/ids that came from the append-only event log to a real terminal. A hand-edited or
    foreign log line can populate ANY string-shaped field (a sprint/work-item id, a name, a note,
    a WorkItem state) with arbitrary bytes, and a capable terminal executing an embedded OSC
    sequence (clipboard/title/hyperlink hijacking) is a real risk, not cosmetic -- this covers
    both the classic 7-bit `ESC ]` form AND the 8-bit C1 form (`U+009D ... U+009C`) some
    terminals also recognize. Tab/newline pass through untouched (expected in prose). ``--json``
    output must NOT be run through this — raw values there are a data contract, not a terminal
    render, and a JSON consumer never executes escape sequences.

    Shared across every command module that prints log-sourced text — do not re-implement this
    locally per module, that's exactly how earlier sinks (sprint id, item id, WorkItem state) got
    missed one at a time across several review rounds while only the first-found field (name)
    had its own private copy.
    """
    return _CONTROL_CHARS.sub(lambda m: repr(m.group())[1:-1], text)


def open_store() -> EventStore:
    """The event store rooted at the resolved store dir (``$PM_HOME`` or the XDG default)."""
    return EventStore(resolve_store_dir())


def machine() -> StateMachine:
    """The active state machine (the default registry for Release 1)."""
    return default_machine()


def load_items(store: EventStore, m: StateMachine | None = None) -> dict[str, WorkItem]:
    """Replay the log into the current work-item projection (reusing ``m`` if the caller has one)."""
    return build_projection(store.read_all(), m or machine())
