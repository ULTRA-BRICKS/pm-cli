"""The intake dead-letter queue — never lose an inbound request, even if processing fails.

WHAT THIS FILE IS
    ``IntakeEvent`` (a raw captured request + its processing status) and ``plan_processing`` (PURE:
    given the current unprocessed intakes and a ``processor`` callable, decide the events to append).

WHY A DLQ (research #255 brainstorm)
    Every inbound request is recorded as ``intake_captured`` (status ``unprocessed``) the instant pm
    sees it — BEFORE any classification/processing — so a crash or a processing bug can never drop
    it. Processing (turning it into a WorkItem) is retried a BOUNDED number of times; each failure
    is an ``intake_failed`` event (attempt counter). When the attempts hit :data:`MAX_ATTEMPTS`, the
    intake moves to ``intake_deadletter`` with the failure reason — one durable record instead of an
    infinite silent retry.

OPERATIONAL MODEL
    ``plan_processing`` advances each unprocessed intake by ONE attempt per call. ``pm ingest`` is
    the tick that calls it, so re-running ingest is what drives retries — a transiently failing
    intake needs up to :data:`MAX_ATTEMPTS` ingest runs to reach ``intake_deadletter``. This matches
    the design's short-lived event-triggered tick (a resident daemon can wrap it later); it is not
    an in-process retry loop.

INVARIANTS
    - ``plan_processing`` is pure: no I/O, no clock. Same ledger + processor -> same events.
    - Only ``unprocessed`` intakes are acted on; ``processed`` / ``deadletter`` are terminal.
    - A processing failure — an exception OR a malformed payload (non-dict / no ``id``) — is caught
      and turned into an ``intake_failed`` / ``intake_deadletter`` event; it never crashes the run,
      so a bad processor can't defeat the DLQ.
    - A successful process emits BOTH the WorkItem ``intake`` event (the item is born) and the
      ``intake_processed`` marker, so the DLQ ledger and the WorkItem queue stay consistent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .events import Event, EventKind, new_event

# How many processing attempts before an intake is dead-lettered (bounded retry).
MAX_ATTEMPTS = 3

# Ledger statuses.
STATUS_UNPROCESSED = "unprocessed"
STATUS_PROCESSED = "processed"
STATUS_DEADLETTER = "deadletter"

# A processor turns a captured intake into a WorkItem ``intake`` payload, or raises to signal a
# (retryable) failure. M3 ships a trivial default; a real classifier slots in here later.
Processor = Callable[["IntakeEvent"], dict]


@dataclass
class IntakeEvent:
    """One captured inbound request and its dead-letter-queue status."""

    id: str
    source: str = ""
    external_id: str = ""
    sender: str = ""
    text: str = ""
    ts_ms: int | None = None
    pane: str = ""
    status: str = STATUS_UNPROCESSED
    attempts: int = 0
    last_error: str = ""
    work_item_id: str = ""
    captured_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        """A JSON-serializable view for ``pm dlq --json``."""
        return {
            "id": self.id, "source": self.source, "external_id": self.external_id,
            "sender": self.sender, "text": self.text, "status": self.status,
            "attempts": self.attempts, "last_error": self.last_error,
            "work_item_id": self.work_item_id,
            "captured_at": self.captured_at, "updated_at": self.updated_at,
        }


def plan_processing(intakes: dict[str, IntakeEvent], processor: Processor) -> list[Event]:
    """The events to append to process every ``unprocessed`` intake once (bounded-retry aware)."""
    events: list[Event] = []
    for intake in intakes.values():
        if intake.status != STATUS_UNPROCESSED:
            continue
        events.extend(_process_one(intake, processor))
    return events


def _process_one(intake: IntakeEvent, processor: Processor) -> list[Event]:
    try:
        payload = processor(intake)
        work_item_id = _require_work_item_id(payload)  # validate INSIDE the guard
    # User-supplied processors may raise any normal exception; each must become a retry/DLQ event.
    except Exception as exc:  # noqa: BLE001 - processor failure boundary; interrupts still propagate
        return [_failure_event(intake, str(exc) or exc.__class__.__name__)]
    return [
        new_event(EventKind.INTAKE, "", payload),  # the WorkItem is born
        new_event(EventKind.INTAKE_PROCESSED, intake.id, {"work_item_id": work_item_id}),
    ]


def _require_work_item_id(payload: object) -> str:
    """Extract the WorkItem id from a processor payload, rejecting a malformed one.

    A processor that returns a non-dict or an id-less payload is a processing FAILURE, not a crash:
    raising here keeps it inside :func:`_process_one`'s guard so the intake is retried/deadlettered
    like any other failure — the DLQ must never be defeated by a bad processor result.
    """
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ValueError("processor returned a payload without an 'id'")
    return str(payload["id"])


def _failure_event(intake: IntakeEvent, reason: str) -> Event:
    # This attempt is the (attempts+1)-th; deadletter once it reaches the bound.
    if intake.attempts + 1 >= MAX_ATTEMPTS:
        return new_event(EventKind.INTAKE_DEADLETTER, intake.id, {"reason": reason})
    return new_event(EventKind.INTAKE_FAILED, intake.id, {"reason": reason})
