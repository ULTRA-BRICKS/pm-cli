"""Fold an event log into the work queue — the deterministic replay at the heart of pm.

WHAT THIS FILE IS
    ``build_projection(events, machine)`` — replays an ordered list of :class:`Event` s into a
    ``{item_id: WorkItem}`` map. This is research #255's first invariant made concrete: the queue
    is a pure function of the log, so a crash/restart reaches the exact same projection.

INVARIANTS
    - **Pure + deterministic.** No I/O, no clock, no randomness. Same events -> same map (asserted
      by ``tests/test_projection.py``).
    - **Lenient on a bad logged transition.** The CLI validates BEFORE it appends, so illegal moves
      should never reach the log; but if one does (a hand-edited log, a future bug), replay records
      it on ``item.errors`` and leaves the state untouched rather than crashing the whole queue.
    - **Identity comes from the event, never generated at replay** — otherwise replay would not be
      deterministic. Intake identity is ``payload.id`` -> ``event.item_id`` -> ``event.id``.
"""

from __future__ import annotations

from .events import Event, EventKind
from .model import PRIVACY_LOCAL_ONLY, EvidenceRef, Transition, WorkItem
from .states import EvidenceError, StateMachine, TransitionError


def build_projection(events: list[Event], machine: StateMachine) -> dict[str, WorkItem]:
    """Replay ``events`` (in order) into a map of work items."""
    items: dict[str, WorkItem] = {}
    for event in events:
        _apply(items, event, machine)
    return items


def _apply(items: dict[str, WorkItem], event: Event, machine: StateMachine) -> None:
    if event.kind == EventKind.INTAKE:
        _apply_intake(items, event, machine)
    elif event.kind == EventKind.TRANSITION:
        _apply_transition(items, event, machine)
    elif event.kind == EventKind.EVIDENCE:
        _apply_evidence(items, event)
    elif event.kind == EventKind.NOTE:
        _apply_note(items, event)
    # Unknown kinds are ignored (forward-compatible with events from a newer pm).


def _intake_id(event: Event) -> str:
    return str(event.payload.get("id") or event.item_id or event.id)


def _apply_intake(items: dict[str, WorkItem], event: Event, machine: StateMachine) -> None:
    item_id = _intake_id(event)
    existing = items.get(item_id)
    if existing is not None:  # a second intake for the same item — just link the reference
        existing.intake_refs.append(event.id)
        existing.updated_at = event.ts
        return
    p = event.payload
    items[item_id] = WorkItem(
        id=item_id,
        title=str(p.get("title", "")),
        state=str(p.get("state") or machine.initial_state),
        project_id=str(p.get("project", "")),
        privacy=str(p.get("privacy") or PRIVACY_LOCAL_ONLY),
        task_refs=[str(r) for r in p.get("task_refs", [])],
        evidence=_intake_evidence(p, event.ts),
        intake_refs=[event.id],
        created_at=event.ts,
        updated_at=event.ts,
    )


def _evidence_ref(data: dict, ts: str) -> EvidenceRef:
    """Build one EvidenceRef from a ``{kind, uri, digest}`` mapping observed at ``ts``."""
    return EvidenceRef(
        kind=str(data.get("kind", "")),
        uri=str(data.get("uri", "")),
        observed_at=ts,
        digest=str(data.get("digest", "")),
    )


def _intake_evidence(payload: dict, ts: str) -> list[EvidenceRef]:
    """Evidence carried inline on an intake event (adapters attach it so onboarding is atomic)."""
    return [_evidence_ref(e, ts) for e in payload.get("evidence", []) if isinstance(e, dict)]


def _apply_transition(items: dict[str, WorkItem], event: Event, machine: StateMachine) -> None:
    item = items.get(event.item_id)
    if item is None:
        return  # transition for an unknown item — nothing to advance
    target = str(event.payload.get("to", ""))
    force = bool(event.payload.get("force", False))
    try:
        machine.validate_transition(
            item.state, target, evidence_kinds=item.evidence_kinds(), force=force
        )
    except (TransitionError, EvidenceError) as exc:
        item.errors.append(f"{event.ts}: rejected {item.state}->{target}: {exc.summary}")
        return
    item.history.append(Transition(from_state=item.state, to=target, ts=event.ts))
    item.state = target
    item.updated_at = event.ts


def _apply_evidence(items: dict[str, WorkItem], event: Event) -> None:
    item = items.get(event.item_id)
    if item is None:
        return
    item.evidence.append(_evidence_ref(event.payload, event.ts))
    item.updated_at = event.ts


def _apply_note(items: dict[str, WorkItem], event: Event) -> None:
    item = items.get(event.item_id)
    if item is None:
        return
    item.notes.append(str(event.payload.get("text", "")))
    item.updated_at = event.ts
