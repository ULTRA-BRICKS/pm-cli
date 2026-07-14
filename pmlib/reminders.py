"""Reminder triggers — pure functions over the projection + a clock.

``compute_reminders`` derives, from the WorkItem map and a ``now``, the nudges the coordinator
should surface (research #255's reminder triggers):

- ``blocker-cleared``   — a ``blocks`` edge whose blocker reached a terminal state while the blocked
  item has not advanced (covers "blocker changed state" AND "child done, parent not advanced").
- ``wait-satisfied``    — a ``waits_for`` edge whose target finished while the waiter has not.
- ``duplicate-open``    — a ``duplicates`` edge where both items are still open.
- ``superseded-open``   — a ``supersedes`` edge whose target is still open (it should be cancelled).
- ``no-progress``       — an ``active``-category item untouched past its SLA / the default window.
- ``timer-expired``     — a non-terminal item with a timer whose ``due_at`` has passed.
- ``dependency-cycle``  — a cycle in the ``blocks`` graph.

Pure: no I/O, no clock of its own (``now`` is passed in), so it is deterministic and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .states import WorkStateCategory

if TYPE_CHECKING:  # annotation-only
    from .model import WorkItem
    from .states import StateMachine

DEFAULT_NO_PROGRESS_SECONDS = 86400  # 24h untouched = stale, unless the state sets its own SLA


@dataclass(frozen=True)
class Reminder:
    """One actionable nudge. ``item_id`` is who to act on; ``target_id`` the related item, if any."""

    kind: str
    item_id: str
    detail: str
    target_id: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "item_id": self.item_id,
                "detail": self.detail, "target_id": self.target_id}


def compute_reminders(items: dict[str, WorkItem], machine: StateMachine, now: datetime,
                      no_progress_after_seconds: int = DEFAULT_NO_PROGRESS_SECONDS) -> list[Reminder]:
    """Every reminder the current projection implies at ``now`` (sorted, deduped)."""
    out: list[Reminder] = []
    for item in items.values():
        out.extend(_edge_reminders(item, items, machine))
        out.extend(_no_progress_reminders(item, machine, now, no_progress_after_seconds))
        out.extend(_timer_reminders(item, machine, now))
    out.extend(_cycle_reminders(items))
    return sorted(set(out), key=lambda r: (r.kind, r.item_id, r.target_id))


def _terminal(machine: StateMachine, state: str) -> bool:
    defn = machine.get(state)
    return bool(defn and defn.terminal)


def _edge_reminders(item: WorkItem, items: dict[str, WorkItem],
                    machine: StateMachine) -> list[Reminder]:
    out: list[Reminder] = []
    for edge in item.dependencies:
        target = items.get(edge.target_id)
        if target is None:
            continue
        out.extend(_one_edge_reminder(item, edge.edge_type, target, machine))
    return out


def _one_edge_reminder(source: WorkItem, edge_type: str, target: WorkItem,
                       machine: StateMachine) -> list[Reminder]:
    src_done = _terminal(machine, source.state)
    tgt_done = _terminal(machine, target.state)
    if edge_type == "blocks" and src_done and not tgt_done:
        return [Reminder("blocker-cleared", target.id,
                         f"blocker {source.id} is done; {target.id} can proceed", source.id)]
    if edge_type == "waits_for" and tgt_done and not src_done:
        return [Reminder("wait-satisfied", source.id,
                         f"{source.id} was waiting on {target.id}, now done", target.id)]
    if edge_type == "duplicates" and not src_done and not tgt_done:
        return [Reminder("duplicate-open", source.id,
                         f"{source.id} duplicates {target.id}; close one", target.id)]
    if edge_type == "supersedes" and not tgt_done:
        return [Reminder("superseded-open", source.id,
                         f"{target.id} is superseded by {source.id} but still open", target.id)]
    return []


def _no_progress_reminders(item: WorkItem, machine: StateMachine, now: datetime,
                           default_seconds: int) -> list[Reminder]:
    defn = machine.get(item.state)
    if defn is None or defn.category is not WorkStateCategory.ACTIVE:
        return []
    updated = _parse_iso(item.updated_at)
    if updated is None:
        return []
    threshold = defn.sla_seconds if defn.sla_seconds is not None else default_seconds
    if (now - updated).total_seconds() <= threshold:
        return []
    return [Reminder("no-progress", item.id, f"{item.id} untouched since {item.updated_at}")]


def _timer_reminders(item: WorkItem, machine: StateMachine, now: datetime) -> list[Reminder]:
    if _terminal(machine, item.state):
        return []  # a finished item's timers no longer matter
    out: list[Reminder] = []
    for timer in item.timers:
        due = _parse_iso(timer.due_at)
        if due is not None and due <= now:
            out.append(Reminder("timer-expired", item.id,
                                f"timer '{timer.reason or timer.kind}' due {timer.due_at}"))
    return out


def _cycle_reminders(items: dict[str, WorkItem]) -> list[Reminder]:
    graph = {item.id: [e.target_id for e in item.dependencies
                       if e.edge_type == "blocks" and e.target_id in items]
             for item in items.values()}
    out: list[Reminder] = []
    for cycle in _find_cycles(graph):
        anchor = min(cycle)
        out.append(Reminder("dependency-cycle", anchor,
                            "blocks cycle: " + " -> ".join(sorted(cycle))))
    return out


def _find_cycles(graph: dict[str, list[str]]) -> set[frozenset[str]]:
    """Every node-set that forms a cycle in the directed ``blocks`` graph (deduped by node set)."""
    cycles: set[frozenset[str]] = set()
    color: dict[str, int] = {}  # 0=unseen, 1=on-stack, 2=done
    path: list[str] = []

    def visit(node: str) -> None:
        color[node] = 1
        path.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt, 0) == 1:  # back-edge -> the slice from nxt is a cycle
                cycles.add(frozenset(path[path.index(nxt):]))
            elif color.get(nxt, 0) == 0:
                visit(nxt)
        path.pop()
        color[node] = 2

    for start in graph:
        if color.get(start, 0) == 0:
            visit(start)
    return cycles


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (tolerant of a ``Z`` suffix / a naive value -> UTC)."""
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
