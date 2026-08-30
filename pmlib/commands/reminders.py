"""``pm reminders`` — surface the nudges the current projection implies (read-only).

Computes reminder triggers (blocker cleared, wait satisfied, redundant duplicate/supersede, no
progress, expired timer, dependency cycle, sprint-lifecycle nudges) from the work-item + sprint
graphs and the current time. This is the "organ of sight" surface for what needs attention — the
SAME surface the ``pm-reminders-push`` cron already pushes to Telegram, so sprint nudges land
there automatically with no new dispatch machinery. It never mutates state.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from ..projection import build_projection
from ..reminders import compute_reminders, compute_sprint_reminders
from ..sprint import build_sprint_projection
from ._shared import display_safe, machine, open_store

NAME = "reminders"
HELP = "show reminder nudges (blocked/unblocked, stale, duplicate, cycle, expired timer, sprint)"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit a JSON array instead of a table")


def run(args: argparse.Namespace) -> int:
    m = machine()
    events = open_store().read_all()
    items = build_projection(events, m)
    now = datetime.now(timezone.utc)
    reminders = compute_reminders(items, m, now)
    reminders += compute_sprint_reminders(build_sprint_projection(events), items, m, now)
    reminders.sort(key=lambda r: (r.kind, r.item_id, r.target_id))
    if args.json:
        print(json.dumps([r.to_dict() for r in reminders], ensure_ascii=False))
        return 0
    if not reminders:
        print("(no reminders)")
        return 0
    kind_w = max(len(r.kind) for r in reminders)
    for r in reminders:
        # `r.item_id` (a work-item OR sprint id) and `r.detail` (a formatted string that embeds
        # that same id) are replayed from the log -- a foreign/hand-edited entry isn't bound by
        # any CLI-side id shape, so both need the same terminal-escape guard as `pm sprint
        # status`/`list` apply to their own id/name/note fields. `--json` above is untouched.
        print(f"{r.kind:<{kind_w}}  {display_safe(r.item_id)}  {display_safe(r.detail)}")
    return 0
