"""``pm record`` — append an observation event to the log (the only write path).

Sub-actions: ``intake`` (a request/work item was seen), ``transition`` (advance state — validated
against the FSM + evidence gate BEFORE it is written, so the log never holds an illegal move),
``evidence`` (attach an evidence ref), ``note`` (free-form context). This is the "organ of sight"
recording what it observed; it does not dispatch or implement anything.
"""

from __future__ import annotations

import argparse

from ..events import EventKind, new_event
from ._shared import load_items, machine, open_store

NAME = "record"
HELP = "record an observation event (intake / transition / evidence / note)"


def configure(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="record_cmd", metavar="<what>", required=True)

    intake = sub.add_parser("intake", help="record that a request/work item was seen")
    intake.add_argument("--id", required=True, help="stable work-item id (deterministic replay)")
    intake.add_argument("--title", default="", help="short human title")
    intake.add_argument("--project", default="", help="repo/project the work belongs to")
    intake.add_argument(
        "--privacy",
        default="local-only",
        choices=["local-only", "remote-allowed"],
        help="privacy class",
    )
    intake.add_argument("--state", default="", help="initial state (default: the machine's seed)")
    intake.add_argument(
        "--task-ref",
        action="append",
        default=[],
        dest="task_refs",
        help="linked task-cli ticket ref (repeatable)",
    )

    tr = sub.add_parser("transition", help="advance a work item to a new state")
    tr.add_argument("id", help="work-item id")
    tr.add_argument("to", help="target state")
    tr.add_argument("--force", action="store_true", help="bypass adjacency + evidence gates")

    ev = sub.add_parser("evidence", help="attach an evidence ref to a work item")
    ev.add_argument("id", help="work-item id")
    ev.add_argument("kind", help="evidence kind (pr, ci, ship-audit, deployment, ...)")
    ev.add_argument("uri", help="pointer to the evidence")

    nt = sub.add_parser("note", help="attach free-form context to a work item")
    nt.add_argument("id", help="work-item id")
    nt.add_argument("text", help="the note")


def run(args: argparse.Namespace) -> int:
    handlers = {"intake": _intake, "transition": _transition, "evidence": _evidence, "note": _note}
    return handlers[args.record_cmd](args)


def _intake(args: argparse.Namespace) -> int:
    store = open_store()
    if args.state and machine().get(args.state) is None:
        # Validate before we write: an unknown seed state would project to a stuck, unadvanceable
        # item ("next: unknown state"). Fail closed at the write boundary, like transitions do.
        print(f"error: unknown initial state `{args.state}`", file=_stderr())
        return 2
    payload = {
        "id": args.id,
        "title": args.title,
        "project": args.project,
        "privacy": args.privacy,
        "task_refs": args.task_refs,
    }
    if args.state:
        payload["state"] = args.state
    store.append(new_event(EventKind.INTAKE, "", payload))
    print(args.id)
    return 0


def _transition(args: argparse.Namespace) -> int:
    store = open_store()
    items = load_items(store)
    item = items.get(args.id)
    if item is None:
        print(f"error: no work item `{args.id}`", file=_stderr())
        return 2
    # Validate against the live projection BEFORE writing — the log must never hold an illegal move.
    machine().validate_transition(
        item.state, args.to, evidence_kinds=item.evidence_kinds(), force=args.force
    )
    store.append(new_event(EventKind.TRANSITION, args.id, {"to": args.to, "force": args.force}))
    print(f"{args.id}: {item.state} -> {args.to}")
    return 0


def _evidence(args: argparse.Namespace) -> int:
    store = open_store()
    items = load_items(store)
    if args.id not in items:
        print(f"error: no work item `{args.id}`", file=_stderr())
        return 2
    store.append(new_event(EventKind.EVIDENCE, args.id, {"kind": args.kind, "uri": args.uri}))
    print(f"{args.id}: +evidence {args.kind}")
    return 0


def _note(args: argparse.Namespace) -> int:
    store = open_store()
    items = load_items(store)
    if args.id not in items:
        print(f"error: no work item `{args.id}`", file=_stderr())
        return 2
    store.append(new_event(EventKind.NOTE, args.id, {"text": args.text}))
    print(f"{args.id}: +note")
    return 0


def _stderr():
    import sys

    return sys.stderr
