"""The Sprint — a lightweight grouping of work items, event-sourced the same way as everything
else in pm (research #255's invariant applies here too: no parallel mutable store).

WHAT THIS FILE IS
    ``Sprint`` + ``SprintItem`` (the projected shape) and ``build_sprint_projection`` (the pure
    fold from the event log into a ``{sprint_id: Sprint}`` map) — the sprint-side mirror of
    ``model.py``/``projection.py``.

INVARIANTS
    - Pure + deterministic, same as ``build_projection``: no I/O, no clock, no randomness.
    - A sprint's lifecycle is linear: ``planning -> active -> closed``. Items can be added only
      while ``planning`` (adding mid-sprint would silently change scope after commitment — if that
      turns out to be wanted, it is a deliberate future decision, not a default).
    - A sprint groups WorkItem ids by reference (``SprintItem.item_id``); it does not duplicate or
      own WorkItem state — ``pm sprint status`` joins against the live WorkItem projection so a
      sprint's view of "done" is never allowed to drift from the item's own FSM state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .events import EventKind

if TYPE_CHECKING:  # annotation-only
    from .events import Event

STATUS_PLANNING = "planning"
STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"


@dataclass
class SprintItem:
    """One work item pulled into a sprint, with its estimate (points/hours — caller's unit)."""

    item_id: str
    estimate: float | None = None
    added_at: str = ""


@dataclass
class Sprint:
    """A sprint: identity, lifecycle status, its pulled-in items, and retro notes."""

    id: str
    name: str = ""
    goal: str = ""
    status: str = STATUS_PLANNING
    items: list[SprintItem] = field(default_factory=list)
    retro_notes: list[str] = field(default_factory=list)
    created_at: str = ""
    started_at: str = ""
    closed_at: str = ""
    updated_at: str = ""

    def item_ids(self) -> list[str]:
        return [i.item_id for i in self.items]

    def total_estimate(self) -> float:
        return sum(i.estimate for i in self.items if i.estimate is not None)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "goal": self.goal,
            "status": self.status,
            "items": [
                {"item_id": i.item_id, "estimate": i.estimate, "added_at": i.added_at}
                for i in self.items
            ],
            "retro_notes": list(self.retro_notes),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "closed_at": self.closed_at,
            "updated_at": self.updated_at,
        }


def build_sprint_projection(events: list[Event]) -> dict[str, Sprint]:
    """Replay ``events`` (in order) into a map of sprints. A second, independent fold over the
    SAME log, exactly like ``build_intake_ledger`` alongside ``build_projection`` — sprint events
    are simply ignored by the WorkItem fold and vice versa (``projection.py``'s ``_apply`` already
    skips unknown kinds, so no change was needed there).
    """
    sprints: dict[str, Sprint] = {}
    for event in events:
        _apply(sprints, event)
    return sprints


def _apply(sprints: dict[str, Sprint], event: Event) -> None:
    if event.kind == EventKind.SPRINT_CREATED:
        _apply_created(sprints, event)
    elif event.kind == EventKind.SPRINT_ITEM_ADDED:
        _apply_item_added(sprints, event)
    elif event.kind == EventKind.SPRINT_STARTED:
        _apply_started(sprints, event)
    elif event.kind == EventKind.SPRINT_CLOSED:
        _apply_closed(sprints, event)
    elif event.kind == EventKind.SPRINT_RETRO:
        _apply_retro(sprints, event)
    # Unknown kinds (including WorkItem-lifecycle events) are ignored — forward-compatible.


def _apply_created(sprints: dict[str, Sprint], event: Event) -> None:
    if not event.item_id:
        # A malformed/foreign log line with no id parses as `""` (events.py), which would become
        # a phantom `sprints[""]` -- reachable by every later lookup, and eventually surfaced as a
        # blank-id `sprint-stale-planning`/`sprint-needs-retro` reminder (real ids never look like
        # this: the CLI always generates `sprint:<uuid4 hex>`).
        return
    if event.item_id in sprints:  # a duplicate create for the same id — ignore, keep the first
        return
    p = event.payload
    sprints[event.item_id] = Sprint(
        id=event.item_id,
        name=str(p.get("name", "")),
        goal=str(p.get("goal", "")),
        created_at=event.ts,
        updated_at=event.ts,
    )


def _coerce_estimate(raw: object) -> float | None:
    """Lenient like the rest of replay (AGENTS.md: one bad line can never crash the queue) — a
    malformed/non-numeric estimate (a hand-edited log line, a future writer's bug) becomes
    ``None`` instead of raising and taking down the whole fold. Non-finite (``nan``/``inf``) and
    negative values are also rejected here as a defense-in-depth backstop — the real gate is the
    CLI refusing to WRITE one in the first place, but a hand-edited or foreign log line isn't
    bound by the CLI's checks (``json.dumps`` happily serializes ``NaN``, which is not valid JSON
    for any strict consumer of the log; a bare negative number is valid JSON but not a valid
    points/hours estimate either — both must never SURVIVE replay even if they land in the log).
    """
    if raw is None or isinstance(raw, bool):
        # `bool` is a `int` subclass in Python -- `float(True)` silently becomes `1.0` rather
        # than the "malformed, discard" `None` a JSON boolean in this field actually deserves.
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a JSON-decoded Python int so large `float(raw)` can't represent it (e.g.
        # a foreign event with a 400-digit "estimate") -- valid JSON, valid Python int, but not a
        # ValueError, and left uncaught this crashed every `read_all()`/sprint read, not just the
        # one malformed line.
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _apply_item_added(sprints: dict[str, Sprint], event: Event) -> None:
    sprint = sprints.get(event.item_id)
    if sprint is None:
        return  # an item added to an unknown sprint — nothing to attach it to
    if sprint.status != STATUS_PLANNING:
        return  # scope is frozen once a sprint has started/closed — ignore a late add
    p = event.payload
    raw_work_item_id = p.get("work_item_id", "")
    if not isinstance(raw_work_item_id, str) or not raw_work_item_id:
        # A malformed/foreign log line (e.g. `"work_item_id": null`) must be discarded, not
        # coerced with `str(...)` -- `str(None)` is the truthy, nonempty string `"None"`, which
        # would silently scope the sprint to a phantom item and could even let it activate.
        return
    work_item_id = raw_work_item_id
    existing = next((i for i in sprint.items if i.item_id == work_item_id), None)
    if existing is not None:  # a re-add re-estimates rather than duplicating the row
        if "estimate" in p:  # a bare re-add (no --estimate) must not wipe the prior value
            coerced = _coerce_estimate(p.get("estimate"))
            prior = existing.estimate or 0.0
            if coerced is not None and math.isfinite(sprint.total_estimate() - prior + coerced):
                existing.estimate = coerced
            # else: either unparseable ("not-a-number") or individually-finite but pushes the
            # sprint's AGGREGATE total non-finite (two `1e308`s summing to `inf`) -- both are
            # discarded, not applied. `total_estimate()` has no remove/edit event to repair it
            # once infected, so the fold itself -- not just the CLI's own `_add` validation --
            # must never let a replayed value push it there; `None` here means "couldn't parse
            # OR would overflow", not "the writer wants no estimate", so an existing valid value
            # is never destructively overwritten (there is no CLI path that sends an explicit
            # null to intentionally clear one; a bare `add` with no --estimate simply omits the
            # key entirely, per the comment above).
        existing.added_at = event.ts
    else:
        estimate = _coerce_estimate(p.get("estimate"))
        if estimate is not None and not math.isfinite(sprint.total_estimate() + estimate):
            estimate = None  # same overflow guard as the re-add branch above
        sprint.items.append(SprintItem(item_id=work_item_id, estimate=estimate, added_at=event.ts))
    sprint.updated_at = event.ts


def _apply_started(sprints: dict[str, Sprint], event: Event) -> None:
    sprint = sprints.get(event.item_id)
    if sprint is None or sprint.status != STATUS_PLANNING:
        return  # only a planning sprint can start
    if not sprint.items:
        # Mirrors commands/sprint.py's own `_start` guard ("has no items -- add first") at the
        # fold level too: the CLI can only ever emit a SPRINT_STARTED for a non-empty sprint, but
        # a hand-edited or foreign log entry isn't bound by that — the fold must not trust the
        # event kind alone and let an empty sprint replay as active.
        return
    sprint.status = STATUS_ACTIVE
    sprint.started_at = event.ts
    sprint.updated_at = event.ts


def _apply_closed(sprints: dict[str, Sprint], event: Event) -> None:
    sprint = sprints.get(event.item_id)
    if sprint is None or sprint.status != STATUS_ACTIVE:
        return  # only an active sprint can close — mirrors _apply_started's planning-only guard
    sprint.status = STATUS_CLOSED
    sprint.closed_at = event.ts
    sprint.updated_at = event.ts


def _apply_retro(sprints: dict[str, Sprint], event: Event) -> None:
    sprint = sprints.get(event.item_id)
    if sprint is None or sprint.status != STATUS_CLOSED:
        # A note recorded before close would permanently (and wrongly) satisfy
        # `sprint-needs-retro`, whose whole point is catching a skipped POST-close retro.
        return
    raw_text = event.payload.get("text", "")
    if not isinstance(raw_text, str):
        # Same malformed-log hazard as `_apply_item_added`: `str(None)` is a truthy `"None"` that
        # would populate `retro_notes` and permanently (and wrongly) suppress the
        # `sprint-needs-retro` safety nudge for a sprint that never got a real note.
        return
    text = raw_text.strip()
    if text:
        sprint.retro_notes.append(text)
    sprint.updated_at = event.ts
