"""The event — the atomic, append-only unit the whole PM state is folded from.

WHAT THIS FILE IS
    The ``Event`` dataclass + the ``EventKind`` vocabulary + a ``new_event`` factory. Pure
    data and stdlib ``json`` only; the store (``store.py``) owns the JSONL file, the projection
    (``projection.py``) folds a list of these into ``WorkItem`` s.

INVARIANTS
    - An event is immutable once appended. State is NEVER mutated in place — every change is a
      new event. This is what makes the queue a deterministic replay (research #255's first
      invariant: crash/restart -> same projection).
    - ``id`` is a random hex so two processes appending concurrently never collide on identity;
      ``ts`` is UTC ISO-8601 so replay order is total and timezone-stable.
    - Serialization is a single JSON object per line (JSONL) — the field order in ``to_json``
      is fixed so a diff of the log is readable and stable.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


class EventKind(str):
    """The event vocabulary. A ``str`` subclass so events serialize as plain strings.

    WorkItem lifecycle: ``intake`` (a WorkItem is born), ``transition`` (state move),
    ``evidence`` (proof of a move), ``note`` (free-form context).

    Intake dead-letter queue (M3): a raw inbound request is captured as ``intake_captured``
    BEFORE any processing (so it can never be lost), then processing either marks it
    ``intake_processed`` (a WorkItem was born from it) or, after bounded retries recorded as
    ``intake_failed``, moves it to ``intake_deadletter``.
    """


class _Kinds:
    INTAKE = EventKind("intake")
    TRANSITION = EventKind("transition")
    EVIDENCE = EventKind("evidence")
    NOTE = EventKind("note")
    # intake dead-letter queue
    INTAKE_CAPTURED = EventKind("intake_captured")
    INTAKE_PROCESSED = EventKind("intake_processed")
    INTAKE_FAILED = EventKind("intake_failed")
    INTAKE_DEADLETTER = EventKind("intake_deadletter")
    # dependency graph
    LINK = EventKind("link")  # a typed dependency edge between two work items
    TIMER = EventKind("timer")  # a scheduled reminder/SLA timer on a work item
    # sprints (M4): a lightweight grouping of work items, event-sourced the same way as everything
    # else here — a sprint has its own id and its events use ``item_id`` for the SPRINT id, not a
    # WorkItem id (``sprint_item_added`` carries the linked work-item id in its payload instead).
    SPRINT_CREATED = EventKind("sprint_created")
    SPRINT_ITEM_ADDED = EventKind("sprint_item_added")
    SPRINT_STARTED = EventKind("sprint_started")
    SPRINT_CLOSED = EventKind("sprint_closed")
    SPRINT_RETRO = EventKind("sprint_retro")

    ALL = frozenset(
        {
            INTAKE,
            TRANSITION,
            EVIDENCE,
            NOTE,
            INTAKE_CAPTURED,
            INTAKE_PROCESSED,
            INTAKE_FAILED,
            INTAKE_DEADLETTER,
            LINK,
            TIMER,
            SPRINT_CREATED,
            SPRINT_ITEM_ADDED,
            SPRINT_STARTED,
            SPRINT_CLOSED,
            SPRINT_RETRO,
        }
    )


# Expose the kinds as attributes on ``EventKind`` for ergonomic ``EventKind.INTAKE`` access.
EventKind.INTAKE = _Kinds.INTAKE
EventKind.TRANSITION = _Kinds.TRANSITION
EventKind.EVIDENCE = _Kinds.EVIDENCE
EventKind.NOTE = _Kinds.NOTE
EventKind.INTAKE_CAPTURED = _Kinds.INTAKE_CAPTURED
EventKind.INTAKE_PROCESSED = _Kinds.INTAKE_PROCESSED
EventKind.INTAKE_FAILED = _Kinds.INTAKE_FAILED
EventKind.INTAKE_DEADLETTER = _Kinds.INTAKE_DEADLETTER
EventKind.LINK = _Kinds.LINK
EventKind.TIMER = _Kinds.TIMER
EventKind.SPRINT_CREATED = _Kinds.SPRINT_CREATED
EventKind.SPRINT_ITEM_ADDED = _Kinds.SPRINT_ITEM_ADDED
EventKind.SPRINT_STARTED = _Kinds.SPRINT_STARTED
EventKind.SPRINT_CLOSED = _Kinds.SPRINT_CLOSED
EventKind.SPRINT_RETRO = _Kinds.SPRINT_RETRO
EventKind.ALL = _Kinds.ALL


def _utc_now_iso() -> str:
    """UTC now as ISO-8601 with a trailing ``Z`` (stable, timezone-explicit, sortable)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Event:
    """One immutable append-only record. ``item_id`` is the work item it targets ("" = global)."""

    id: str
    ts: str
    kind: str
    item_id: str
    payload: dict = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize to a single-line JSON object with a fixed key order (stable log diffs)."""
        return json.dumps(
            {
                "id": self.id,
                "ts": self.ts,
                "kind": self.kind,
                "item_id": self.item_id,
                "payload": self.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> "Event":
        """Parse one JSONL record. Raises ``ValueError``/``KeyError`` on a malformed line."""
        raw = json.loads(line)
        return cls(
            id=str(raw["id"]),
            ts=str(raw["ts"]),
            kind=str(raw["kind"]),
            item_id=str(raw.get("item_id", "")),
            payload=dict(raw.get("payload") or {}),
        )


def new_event(kind: str, item_id: str, payload: dict | None = None) -> Event:
    """Mint a fresh event with a random id and a UTC timestamp."""
    return Event(
        id=uuid.uuid4().hex,
        ts=_utc_now_iso(),
        kind=kind,
        item_id=item_id,
        payload=dict(payload or {}),
    )
