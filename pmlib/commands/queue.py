"""``pm queue`` — render the current work queue (the projected event log).

Read-only. Replays the log into work items and prints them as a table (human) or a JSON array
(``--json``, for tooling). Optional ``--category`` / ``--state`` filters. This is the primary
"organ of sight" surface: what is in flight, in what state, with what evidence.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

from ..labels import pm_health, pm_labels
from ._shared import load_items, machine, open_store

if TYPE_CHECKING:  # annotation-only imports
    from ..model import WorkItem
    from ..states import StateMachine

NAME = "queue"
HELP = "list the current work queue (projected from the event log)"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit a JSON array instead of a table")
    parser.add_argument("--category", default="", help="filter by state category")
    parser.add_argument("--state", default="", help="filter by exact state id")


def run(args: argparse.Namespace) -> int:
    m = machine()
    items = list(load_items(open_store(), m).values())
    items = _filter(items, args, m)
    items.sort(key=lambda it: (it.updated_at, it.id))
    if args.json:
        rows = [{**it.to_dict(), "labels": pm_labels(it, m)} for it in items]
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    _print_table(items, m)
    return 0


def _filter(items: list[WorkItem], args: argparse.Namespace, m: StateMachine) -> list[WorkItem]:
    states = m.states
    out = items
    if args.state:
        out = [it for it in out if it.state == args.state]
    if args.category:
        out = [
            it for it in out if (d := states.get(it.state)) and d.category.value == args.category
        ]
    return out


def _print_table(items: list[WorkItem], m: StateMachine) -> None:
    if not items:
        print("(queue empty)")
        return
    rows = [(it.id, it.state, pm_health(it, m), it) for it in items]
    id_w = max((len(r[0]) for r in rows), default=2)
    state_w = max((len(r[1]) for r in rows), default=12)
    health_w = max((len(r[2]) for r in rows), default=7)
    for item_id, state, health, it in rows:
        flags = f"  !{len(it.errors)}" if it.errors else ""
        print(
            f"{item_id:<{id_w}}  {state:<{state_w}}  {health:<{health_w}}  {it.title[:60]}{flags}"
        )
