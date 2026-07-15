"""``pm ingest`` — onboard work items from an existing tool into the pm queue.

Two sources, dispatched by ``--source``:
- ``task``: the task-cli adapter — durable tickets project straight into deduped WorkItems.
- ``telegram``: inbound Telegram messages go through the intake DEAD-LETTER QUEUE — each is captured
  as ``intake_captured`` (unprocessed) the instant pm sees it, then processed into a WorkItem with
  bounded retries; repeated failures dead-letter instead of being lost.

``--dry-run`` previews without writing; ``--from-file`` reads records from a JSON file instead of
shelling out (piping + hermetic tests). This is observation, not dispatch.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..adapters import task as task_adapter
from ..adapters import telegram as tg_adapter
from ..adapters.task import fetch_task_records, flatten_records, plan_ingest
from ..adapters.telegram import default_processor, fetch_tg_records, plan_capture
from ..events import EventKind
from ..intake import plan_processing
from ..projection import build_intake_ledger
from ._shared import load_items, machine, open_store

NAME = "ingest"
HELP = "onboard work items from an existing tool (--source task|telegram)"

# Structured exit code for an adapter/fetch failure (tool missing, bad --repo-dir, bad JSON).
EXIT_ADAPTER_ERROR = 4

_SOURCES = (task_adapter.SOURCE, tg_adapter.SOURCE)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True, choices=_SOURCES, help="the tool to ingest from")
    parser.add_argument("--repo-dir", default="", dest="repo_dir",
                        help="(task) repo DIRECTORY to run the task fetch in (task resolves the "
                        "project from cwd). Named -dir to avoid task-cli's --repo (owner/name).")
    parser.add_argument("--since", default="",
                        help="(telegram) only messages at/after this tg date (ISO or relative 3d)")
    parser.add_argument("--from-file", default="", dest="from_file",
                        help="read records from a JSON file instead of shelling out")
    parser.add_argument("--dry-run", action="store_true", help="preview the plan, write nothing")
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")


def run(args: argparse.Namespace) -> int:
    try:
        if args.source == tg_adapter.SOURCE:
            return _run_telegram(args)
        if args.source == task_adapter.SOURCE:
            return _run_task(args)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ADAPTER_ERROR
    print(f"error: unsupported source `{args.source}`", file=sys.stderr)
    return EXIT_ADAPTER_ERROR


# ── task source ─────────────────────────────────────────────────────────────────────────
def _run_task(args: argparse.Namespace) -> int:
    records = _load_file(args) if args.from_file else fetch_task_records(cwd=args.repo_dir)
    records = flatten_records(records)
    store = open_store()
    m = machine()
    events = plan_ingest(records, load_items(store, m), m)  # project comes from each ticket's group
    new_items = sum(1 for e in events if e.kind == EventKind.INTAKE)
    if not args.dry_run:
        for event in events:
            store.append(event)
    _report_task(args, new_items, len(records))
    return 0


# ── telegram source (dead-letter queue) ───────────────────────────────────────────────────
def _run_telegram(args: argparse.Namespace) -> int:
    records = _load_file(args) if args.from_file else fetch_tg_records(since=args.since)
    store = open_store()
    capture_events = plan_capture(records, build_intake_ledger(store.read_all()))
    if args.dry_run:
        _report_tg(args, captured=len(capture_events), processed=0, deadlettered=[])
        return 0
    for event in capture_events:
        store.append(event)
    process_events = plan_processing(build_intake_ledger(store.read_all()), default_processor)
    for event in process_events:
        store.append(event)
    deadlettered = [(e.item_id, e.payload.get("reason", "")) for e in process_events
                    if e.kind == EventKind.INTAKE_DEADLETTER]
    processed = sum(1 for e in process_events if e.kind == EventKind.INTAKE_PROCESSED)
    _report_tg(args, captured=len(capture_events), processed=processed, deadlettered=deadlettered)
    return 0


# ── shared ────────────────────────────────────────────────────────────────────────────────
def _load_file(args: argparse.Namespace) -> list[dict]:
    import pathlib

    data = json.loads(pathlib.Path(args.from_file).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{args.from_file}: expected a JSON array of records")
    return [r for r in data if isinstance(r, dict)]


def _report_task(args: argparse.Namespace, new_items: int, scanned: int) -> None:
    if args.json:
        print(json.dumps({"source": "task", "scanned": scanned,
                          "new_items": new_items, "dry_run": args.dry_run}))
        return
    verb = "would onboard" if args.dry_run else "onboarded"
    print(f"task: scanned {scanned}, {verb} {new_items} new item(s)")


def _report_tg(args: argparse.Namespace, captured: int, processed: int,
               deadlettered: list[tuple[str, str]]) -> None:
    if args.json:
        print(json.dumps({"source": "telegram", "captured": captured, "processed": processed,
                          "deadlettered": len(deadlettered), "dry_run": args.dry_run}))
        return
    verb = "would capture" if args.dry_run else "captured"
    print(f"telegram: {verb} {captured} new message(s), processed {processed}")
    for intake_id, reason in deadlettered:  # the one durable report for exhausted retries
        print(f"  DEADLETTER {intake_id}: {reason}")
