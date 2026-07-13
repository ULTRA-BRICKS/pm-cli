"""``pm queue`` — render the current work queue (the projected event log).

Read-only. Replays the log into work items and prints them as a table (human) or a JSON array
(``--json``, for tooling). Optional ``--category`` / ``--state`` filters. This is the primary
"organ of sight" surface: what is in flight, in what state, with what evidence.
"""

from __future__ import annotations

import argparse
import json

from ..model import WorkItem
from ._shared import load_items, machine, open_store

NAME = "queue"
HELP = "list the current work queue (projected from the event log)"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit a JSON array instead of a table")
    parser.add_argument("--category", default="", help="filter by state category")
    parser.add_argument("--state", default="", help="filter by exact state id")


def run(args: argparse.Namespace) -> int:
    items = list(load_items(open_store()).values())
    items = _filter(items, args)
    items.sort(key=lambda it: (it.updated_at, it.id))
    if args.json:
        print(json.dumps([it.to_dict() for it in items], ensure_ascii=False))
        return 0
    _print_table(items)
    return 0


def _filter(items: list[WorkItem], args: argparse.Namespace) -> list[WorkItem]:
    states = machine().states
    out = items
    if args.state:
        out = [it for it in out if it.state == args.state]
    if args.category:
        out = [
            it for it in out if (d := states.get(it.state)) and d.category.value == args.category
        ]
    return out


def _print_table(items: list[WorkItem]) -> None:
    if not items:
        print("(queue empty)")
        return
    id_w = max((len(it.id) for it in items), default=2)
    state_w = max((len(it.state) for it in items), default=12)
    for it in items:
        flags = f"  !{len(it.errors)}" if it.errors else ""
        title = it.title[:60]
        print(f"{it.id:<{id_w}}  {it.state:<{state_w}}  {title}{flags}")
