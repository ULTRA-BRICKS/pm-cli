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

    Kept deliberately small for Release 1 — the "organ of sight" only needs to record what it
    observed (intake), how work moved (transition), what proved a move (evidence), and free-form
    context (note). Dispatch/dependency events arrive in later milestones.
    """


class _Kinds:
    INTAKE = EventKind("intake")
    TRANSITION = EventKind("transition")
    EVIDENCE = EventKind("evidence")
    NOTE = EventKind("note")

    ALL = frozenset({INTAKE, TRANSITION, EVIDENCE, NOTE})


# Expose the kinds as attributes on ``EventKind`` for ergonomic ``EventKind.INTAKE`` access.
EventKind.INTAKE = _Kinds.INTAKE
EventKind.TRANSITION = _Kinds.TRANSITION
EventKind.EVIDENCE = _Kinds.EVIDENCE
EventKind.NOTE = _Kinds.NOTE
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
            {"id": self.id, "ts": self.ts, "kind": self.kind,
             "item_id": self.item_id, "payload": self.payload},
            ensure_ascii=False, separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> "Event":
        """Parse one JSONL record. Raises ``ValueError``/``KeyError`` on a malformed line."""
        raw = json.loads(line)
        return cls(
            id=str(raw["id"]), ts=str(raw["ts"]), kind=str(raw["kind"]),
            item_id=str(raw.get("item_id", "")), payload=dict(raw.get("payload") or {}),
        )


def new_event(kind: str, item_id: str, payload: dict | None = None) -> Event:
    """Mint a fresh event with a random id and a UTC timestamp."""
    return Event(id=uuid.uuid4().hex, ts=_utc_now_iso(), kind=kind,
                 item_id=item_id, payload=dict(payload or {}))
