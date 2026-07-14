"""``pm why <id>`` — explain a work item's current state, history, and what unblocks it.

Read-only. Research #255 lists ``pm why`` as a Release-1 surface: given the projection, say WHY
an item sits where it does — its state, the legal next moves, the evidence it still needs, and any
recorded inconsistencies. No dispatch, no mutation.
"""

from __future__ import annotations

import argparse
import sys

from ..labels import pm_labels
from ..model import WorkItem
from ..states import StateMachine, WorkStateDefinition
from ._shared import load_items, machine, open_store

NAME = "why"
HELP = "explain a work item: current state, legal next moves, missing evidence"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("id", help="work-item id")


def run(args: argparse.Namespace) -> int:
    m = machine()
    items = load_items(open_store(), m)
    item = items.get(args.id)
    if item is None:
        print(f"error: no work item `{args.id}`", file=sys.stderr)
        return 2
    defn = m.get(item.state)
    print(f"{item.id}  [{item.state}]  {item.title}")
    if item.project_id:
        print(f"  project:  {item.project_id}")
    print(f"  privacy:  {item.privacy}")
    print(f"  labels:   {', '.join(pm_labels(item, m))}")
    _print_history(item)
    _print_evidence(item)
    _print_deps(item)
    _print_next(m, defn, item)
    _print_errors(item)
    return 0


def _print_deps(item: WorkItem) -> None:
    for edge in item.dependencies:
        suffix = f" ({edge.reason})" if edge.reason else ""
        print(f"  dep:      {edge.edge_type} {edge.target_id}{suffix}")
    for timer in item.timers:
        print(f"  timer:    {timer.kind} @ {timer.due_at}")


def _print_history(item: WorkItem) -> None:
    if item.history:
        chain = " -> ".join([item.history[0].from_state] + [h.to for h in item.history])
        print(f"  path:     {chain}")


def _print_evidence(item: WorkItem) -> None:
    if item.evidence:
        kinds = ", ".join(sorted(item.evidence_kinds()))
        print(f"  evidence: {kinds}")


def _print_next(m: StateMachine, defn: WorkStateDefinition | None, item: WorkItem) -> None:
    if defn is None:
        print("  next:     (unknown state — not in the machine)")
        return
    if defn.terminal:
        print("  next:     (terminal — no further moves)")
        return
    have = item.evidence_kinds()
    parts = []
    for nxt in defn.allowed_next:
        target = m.get(nxt)
        missing = [k for k in (target.evidence_required if target else []) if k not in have]
        parts.append(f"{nxt} (needs {', '.join(missing)})" if missing else nxt)
    print(f"  next:     {', '.join(parts)}")


def _print_errors(item: WorkItem) -> None:
    for err in item.errors:
        print(f"  ! {err}")
