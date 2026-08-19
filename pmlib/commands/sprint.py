"""``pm sprint`` — create/plan/start/status/close/retro a sprint (M4).

A sprint groups work items into a time-boxed batch: ``create`` (name a sprint), ``add`` (pull a
work item in + estimate it — repeatable, "plan" as Alex asked for is this done N times), ``start``
(commit the scope, freeze further ``add``s), ``status`` (read-only progress, joined live against
the WorkItem projection so it never drifts), ``close`` (mark it done), ``retro`` (record retro
notes, or print the retro report with no ``--note``). Same "pm never implements" boundary as the
rest of the tool: this only records observations about a grouping; execution still happens via
``pm record transition`` on the underlying work items, same as always.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid

from ..events import EventKind, new_event
from ..projection import build_projection
from ..sprint import STATUS_ACTIVE, STATUS_CLOSED, STATUS_PLANNING, Sprint, build_sprint_projection
from ._shared import machine, open_store

NAME = "sprint"
HELP = "create/plan/start/status/close/retro a sprint"


def configure(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="sprint_cmd", metavar="<what>", required=True)

    create = sub.add_parser("create", help="create a new sprint")
    create.add_argument("--name", required=True, help="short human name")
    create.add_argument("--goal", default="", help="what this sprint is trying to accomplish")

    add = sub.add_parser("add", help="pull a work item into a planning sprint, with an estimate")
    add.add_argument("sprint_id", help="sprint id (from `pm sprint create`)")
    add.add_argument("item_id", help="work-item id to pull in")
    add.add_argument("--estimate", type=float, default=None, help="points/hours (caller's unit)")

    start = sub.add_parser("start", help="commit a sprint's scope (freezes further `add`s)")
    start.add_argument("sprint_id")

    status = sub.add_parser("status", help="a sprint's progress, joined against live item states")
    status.add_argument("sprint_id")
    status.add_argument("--json", action="store_true")

    close = sub.add_parser("close", help="close a sprint")
    close.add_argument("sprint_id")

    retro = sub.add_parser("retro", help="record a retro note, or print the retro summary")
    retro.add_argument("sprint_id")
    retro.add_argument("--note", default="", help="a retro note to record (omit to just view)")
    retro.add_argument("--json", action="store_true")

    listp = sub.add_parser("list", help="list all known sprints")
    listp.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    handlers = {
        "create": _create,
        "add": _add,
        "start": _start,
        "status": _status,
        "close": _close,
        "retro": _retro,
        "list": _list,
    }
    return handlers[args.sprint_cmd](args)


def _read() -> tuple[dict, dict]:
    """Read the log ONCE and fold both projections from it (mirrors ``commands/reminders.py``'s
    single-read pattern) — avoids the previous 2-3x re-read-and-refold per command invocation.
    """
    events = open_store().read_all()
    return build_sprint_projection(events), build_projection(events, machine())


def _create(args: argparse.Namespace) -> int:
    sprint_id = f"sprint:{uuid.uuid4().hex[:10]}"
    sprints, _ = _read()
    if sprint_id in sprints:  # a uuid4 collision — astronomically unlikely, still never silent
        print(f"error: sprint id `{sprint_id}` collided, retry", file=sys.stderr)
        return 2
    open_store().append(
        new_event(EventKind.SPRINT_CREATED, sprint_id, {"name": args.name, "goal": args.goal})
    )
    print(sprint_id)
    return 0


def _add(args: argparse.Namespace) -> int:
    sprints, items = _read()
    sprint = sprints.get(args.sprint_id)
    if sprint is None:
        print(f"error: no sprint `{args.sprint_id}`", file=sys.stderr)
        return 2
    if sprint.status != STATUS_PLANNING:
        print(
            f"error: sprint `{args.sprint_id}` is `{sprint.status}`, not `planning` "
            f"(scope is frozen once a sprint starts)",
            file=sys.stderr,
        )
        return 2
    if args.item_id not in items:
        print(f"error: no work item `{args.item_id}`", file=sys.stderr)
        return 2
    if args.estimate is not None and not math.isfinite(args.estimate):
        # Reject at the write boundary — json.dumps happily serializes NaN/Infinity, which is
        # not valid JSON for any strict consumer of the append-only log.
        print(f"error: --estimate must be a finite number (got {args.estimate!r})", file=sys.stderr)
        return 2
    payload = {"work_item_id": args.item_id}
    if args.estimate is not None:
        payload["estimate"] = args.estimate
    open_store().append(new_event(EventKind.SPRINT_ITEM_ADDED, args.sprint_id, payload))
    est = f" ({args.estimate})" if args.estimate is not None else ""
    print(f"{args.sprint_id}: +{args.item_id}{est}")
    return 0


def _start(args: argparse.Namespace) -> int:
    sprints, _ = _read()
    sprint = sprints.get(args.sprint_id)
    if sprint is None:
        print(f"error: no sprint `{args.sprint_id}`", file=sys.stderr)
        return 2
    if sprint.status != STATUS_PLANNING:
        print(f"error: sprint `{args.sprint_id}` is already `{sprint.status}`", file=sys.stderr)
        return 2
    if not sprint.items:
        print(
            f"error: sprint `{args.sprint_id}` has no items — `pm sprint add` first",
            file=sys.stderr,
        )
        return 2
    open_store().append(new_event(EventKind.SPRINT_STARTED, args.sprint_id, {}))
    print(f"{args.sprint_id}: {STATUS_PLANNING} -> {STATUS_ACTIVE}")
    return 0


def _close(args: argparse.Namespace) -> int:
    sprints, _ = _read()
    sprint = sprints.get(args.sprint_id)
    if sprint is None:
        print(f"error: no sprint `{args.sprint_id}`", file=sys.stderr)
        return 2
    if sprint.status != STATUS_ACTIVE:
        # The lifecycle is planning -> active -> closed (pmlib/sprint.py docstring); closing a
        # never-started sprint would also fire `sprint-needs-retro` for work that never ran.
        print(
            f"error: sprint `{args.sprint_id}` is `{sprint.status}`, not `active` "
            f"(`pm sprint start` it first)",
            file=sys.stderr,
        )
        return 2
    open_store().append(new_event(EventKind.SPRINT_CLOSED, args.sprint_id, {}))
    print(f"{args.sprint_id}: -> {STATUS_CLOSED}")
    return 0


def _status(args: argparse.Namespace) -> int:
    sprints, items = _read()
    sprint = sprints.get(args.sprint_id)
    if sprint is None:
        print(f"error: no sprint `{args.sprint_id}`", file=sys.stderr)
        return 2
    rows = _status_rows(sprint, items, machine())
    if args.json:
        print(json.dumps(_envelope(sprint, rows), ensure_ascii=False))
        return 0
    _print_status(sprint, rows)
    return 0


def _envelope(sprint: Sprint, rows: list[dict]) -> dict:
    """The one JSON shape ``status``/``retro`` both use — ``rows`` (the live join) is the ONLY
    view of items; the fold's own static ``items`` snapshot is intentionally omitted so a
    consumer can't accidentally read stale state next to fresh state in the same payload.
    """
    body = sprint.to_dict()
    body.pop("items", None)
    return {"sprint": body, "rows": rows}


def _status_rows(sprint: Sprint, wi: dict, m) -> list[dict]:
    rows = []
    for si in sprint.items:
        item = wi.get(si.item_id)
        rows.append(
            {
                "item_id": si.item_id,
                "estimate": si.estimate,
                "state": item.state if item else None,  # honest JSON value; "(unknown)" is a
                "done": bool(item and (d := m.get(item.state)) and d.terminal),  # display concern
            }
        )
    return rows


def _print_status(sprint: Sprint, rows: list[dict]) -> None:
    print(f"# {sprint.id}  {sprint.name}  [{sprint.status}]")
    if sprint.goal:
        print(f"  goal: {sprint.goal}")
    done = sum(1 for r in rows if r["done"])
    print(f"  items: {done}/{len(rows)} done   estimate total: {sprint.total_estimate()}")
    for r in rows:
        mark = "x" if r["done"] else " "
        est = f" ({r['estimate']})" if r["estimate"] is not None else ""
        print(f"  [{mark}] {r['item_id']:<20} {r['state'] or '(unknown)'}{est}")


def _retro(args: argparse.Namespace) -> int:
    sprints, items = _read()
    sprint = sprints.get(args.sprint_id)
    if sprint is None:
        print(f"error: no sprint `{args.sprint_id}`", file=sys.stderr)
        return 2
    note = args.note.strip()  # whitespace-only must not silently pass as a "real" retro note —
    # it would permanently satisfy `sprint-needs-retro` without anyone having actually said anything
    if note:
        if sprint.status != STATUS_CLOSED:
            # A pre-close note would permanently (and wrongly) satisfy `sprint-needs-retro`,
            # whose point is catching a skipped POST-close retro — see pmlib/sprint.py.
            print(
                f"error: sprint `{args.sprint_id}` is `{sprint.status}`, not `closed` "
                f"(`pm sprint close` it first — retro happens after the sprint ends)",
                file=sys.stderr,
            )
            return 2
        open_store().append(new_event(EventKind.SPRINT_RETRO, args.sprint_id, {"text": note}))
        print(f"{args.sprint_id}: +retro note")
        return 0
    rows = _status_rows(sprint, items, machine())
    if args.json:
        print(json.dumps(_envelope(sprint, rows), ensure_ascii=False))
        return 0
    _print_status(sprint, rows)
    print(f"  retro notes: {len(sprint.retro_notes)}")
    for note in sprint.retro_notes:
        print(f"    - {note}")
    return 0


def _list(args: argparse.Namespace) -> int:
    sprints, _ = _read()
    rows = sorted(sprints.values(), key=lambda s: s.created_at)
    if args.json:
        print(json.dumps([s.to_dict() for s in rows], ensure_ascii=False))
        return 0
    if not rows:
        print("(no sprints)")
        return 0
    for s in rows:
        print(f"{s.id:<24} {s.status:<10} {len(s.items):>3} item(s)  {s.name}")
    return 0
