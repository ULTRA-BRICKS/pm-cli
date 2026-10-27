"""``pm route`` — recommend an executor for work (read-only).

``pm route [<id>]`` recommends an executor for one item (or every non-terminal item) from the
executor ledger and the item's risk + privacy. It writes NO events and dispatches nothing — the
verdict (route / wait / blocked) is advisory. ``--risk`` overrides the label-derived tier.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..executors import recommend_routing
from ..states import default_machine
from ._shared import load_items, open_store
from .executors import load_executors

NAME = "route"
HELP = "recommend an executor for work items (read-only; dispatches nothing)"

_RISKS = ("low", "normal", "high")


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("id", nargs="?", default="", help="a work-item id (default: all open items)")
    parser.add_argument("--risk", default="", choices=("", *_RISKS), help="override the risk tier")
    parser.add_argument("--from-file", default="", dest="from_file",
                        help="executor snapshot (JSON array) instead of tg-ctl status")
    parser.add_argument("--json", action="store_true", help="emit a JSON array/object")


def run(args: argparse.Namespace) -> int:
    machine = default_machine()
    items = load_items(open_store(), machine)
    targets = _select_items(items, args.id, machine)
    if targets is None:
        print(f"error: no work item `{args.id}`", file=sys.stderr)
        return 2
    executors = load_executors(args.from_file)
    risk = args.risk or None
    recs = [recommend_routing(item, executors, risk=risk) for item in targets]
    _report(args, recs)
    return 0


def _select_items(items: dict, item_id: str, machine) -> list | None:
    if item_id:
        item = items.get(item_id)
        return None if item is None else [item]
    # all non-terminal items — the ones that could still need an executor
    open_items = [it for it in items.values()
                  if not (d := machine.get(it.state)) or not d.terminal]
    return sorted(open_items, key=lambda it: it.id)


def _report(args: argparse.Namespace, recs: list) -> None:
    if args.id and args.json:
        print(json.dumps(recs[0].to_dict()) if recs else "null")
        return
    if args.json:
        print(json.dumps([r.to_dict() for r in recs], ensure_ascii=False))
        return
    if not recs:
        print("(no open items)")
        return
    id_w = max(len(r.item_id) for r in recs)
    for r in recs:
        tgt = r.executor_id or "-"
        print(f"{r.item_id:<{id_w}}  {r.action:<8}  {r.risk:<6}  {tgt}  {r.reason}")
