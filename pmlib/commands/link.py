"""``pm link`` — record a typed dependency edge between two work items.

``pm link <from> <type> <target> [--reason R]`` where ``<type>`` is one of blocks / waits_for /
duplicates / supersedes / relates_to. Both items must already exist. The edge is validated before
it is written and folded into the source item's ``dependencies`` on replay.
"""

from __future__ import annotations

import argparse
import sys

from ..events import EventKind, new_event
from ..model import EDGE_TYPES
from ._shared import load_items, open_store

NAME = "link"
HELP = "record a typed dependency edge between two work items"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("from_id", metavar="from", help="the source work-item id")
    parser.add_argument("edge_type", metavar="type", choices=EDGE_TYPES,
                        help=f"edge type ({', '.join(EDGE_TYPES)})")
    parser.add_argument("target_id", metavar="target", help="the target work-item id")
    parser.add_argument("--reason", default="", help="why the edge exists")


def run(args: argparse.Namespace) -> int:
    store = open_store()
    items = load_items(store)
    for role, item_id in (("from", args.from_id), ("target", args.target_id)):
        if item_id not in items:
            print(f"error: no {role} work item `{item_id}`", file=sys.stderr)
            return 2
    store.append(new_event(EventKind.LINK, args.from_id, {
        "type": args.edge_type, "target_id": args.target_id, "reason": args.reason}))
    print(f"{args.from_id} {args.edge_type} {args.target_id}")
    return 0
