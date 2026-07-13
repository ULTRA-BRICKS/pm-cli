"""The PM projection labels — how pm richness maps onto task-cli WITHOUT new task states.

Research #255 (and the M2 decision) is explicit: keep task-cli's five canonical states stable
and project pm's richer lifecycle through LABELS/comments only. This module computes that label
set purely from a ``WorkItem``'s pm state — no task-cli mutation happens here; the labels are a
view the ingest/report surfaces render, and (later) a delegated worker can write back.

Labels emitted:
- ``pm.stage:<category>``  — the coarse lifecycle phase (intake/triage/.../exception). Coarse on
  purpose: it does not churn on every micro-transition, so it is stable enough to live on a ticket.
- ``pm.blocked:<state>``   — only when the state's structural ``blocked`` flag is set (stalled on a
  blocker); a transient wait like limit-wait is NOT labelled blocked.
- ``pm.health:<status>``   — healthy | blocked | stuck | waiting | done | cancelled | parked |
  superseded | unknown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .states import WorkStateCategory  # runtime-used (category comparison)

if TYPE_CHECKING:  # annotation-only imports
    from .model import WorkItem
    from .states import StateMachine, WorkStateDefinition

# Terminal / distinguished states whose health is their own name rather than a generic bucket.
_TERMINAL_HEALTH = {
    "done": "done",
    "retro-done": "done",
    "cancelled": "cancelled",
    "parked": "parked",
    "superseded": "superseded",
}


def pm_labels(item: WorkItem, machine: StateMachine) -> list[str]:
    """The pm.stage / pm.blocked / pm.health labels for an item, computed from its state."""
    defn = machine.get(item.state)
    stage = defn.category.value if defn else "unknown"
    labels = [f"pm.stage:{stage}"]
    if defn is not None and defn.blocked:
        labels.append(f"pm.blocked:{item.state}")
    labels.append(f"pm.health:{_health(item, defn)}")
    return labels


def pm_health(item: WorkItem, machine: StateMachine) -> str:
    """Just the health status — the compact signal the queue table shows without the full labels."""
    return _health(item, machine.get(item.state))


def _health(item: WorkItem, defn: WorkStateDefinition | None) -> str:
    if defn is None:
        return "unknown"
    if item.state == "stuck":  # a more specific signal than generic blocked
        return "stuck"
    if defn.blocked:
        return "blocked"
    if item.state in _TERMINAL_HEALTH:
        return _TERMINAL_HEALTH[item.state]
    if defn.category is WorkStateCategory.EXCEPTION:  # limit-wait / provider-overload: transient
        return "waiting"
    return "healthy"
