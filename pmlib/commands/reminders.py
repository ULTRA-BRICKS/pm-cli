"""``pm reminders`` — surface the nudges the current projection implies (read-only).

Computes reminder triggers (blocker cleared, wait satisfied, redundant duplicate/supersede, no
progress, expired timer, dependency cycle) from the work-item graph and the current time. This is
the "organ of sight" surface for what needs attention; it never mutates state.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from ..reminders import compute_reminders
from ._shared import load_items, machine, open_store

NAME = "reminders"
HELP = "show reminder nudges (blocked/unblocked, stale, duplicate, cycle, expired timer)"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit a JSON array instead of a table")


def run(args: argparse.Namespace) -> int:
    m = machine()
    reminders = compute_reminders(load_items(open_store(), m), m, datetime.now(timezone.utc))
    if args.json:
        print(json.dumps([r.to_dict() for r in reminders], ensure_ascii=False))
        return 0
    if not reminders:
        print("(no reminders)")
        return 0
    kind_w = max(len(r.kind) for r in reminders)
    for r in reminders:
        print(f"{r.kind:<{kind_w}}  {r.item_id}  {r.detail}")
    return 0
