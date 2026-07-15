"""``pm timer`` — set a scheduled reminder/SLA timer on a work item.

``pm timer <id> --at <iso> [--reason R] [--kind reminder]``. The timer fires (via ``pm reminders``)
once ``--at`` passes and the item is not terminal. The item must exist and ``--at`` must be a valid
ISO-8601 timestamp.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from ..events import EventKind, new_event
from ._shared import load_items, open_store

NAME = "timer"
HELP = "set a reminder/SLA timer on a work item (fires via pm reminders)"

_KINDS = ("reminder", "sla", "retry", "quota-reset", "review-dwell")


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("id", help="the work-item id")
    parser.add_argument("--at", required=True, help="when it is due (ISO-8601, e.g. 2026-07-20T09:00Z)")
    parser.add_argument("--kind", default="reminder", choices=_KINDS, help="timer kind")
    parser.add_argument("--reason", default="", help="what the timer is for")


def run(args: argparse.Namespace) -> int:
    store = open_store()
    if args.id not in load_items(store):
        print(f"error: no work item `{args.id}`", file=sys.stderr)
        return 2
    if not _valid_iso(args.at):
        print(f"error: --at is not a valid ISO-8601 timestamp: {args.at}", file=sys.stderr)
        return 2
    store.append(new_event(EventKind.TIMER, args.id,
                           {"kind": args.kind, "due_at": args.at, "reason": args.reason}))
    print(f"{args.id}: timer {args.kind} @ {args.at}")
    return 0


def _valid_iso(value: str) -> bool:
    try:
        datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return True
