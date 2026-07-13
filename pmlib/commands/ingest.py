"""``pm ingest`` — onboard work items from an existing tool into the pm queue.

Release 1 supports ``--source task`` (the task-cli adapter). Reads ticket records, plans the
deduped ingest events, and appends them. ``--dry-run`` previews without writing; ``--from-file``
reads records from a JSON file instead of shelling out (used for piping and hermetic tests). This
is observation, not dispatch: it only records what tickets exist.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..adapters import task as task_adapter
from ..adapters.task import fetch_task_records, flatten_records, plan_ingest
from ..events import EventKind
from ._shared import load_items, machine, open_store

NAME = "ingest"
HELP = "onboard work items from an existing tool (--source task)"

# Structured exit code for an adapter/fetch failure (task missing, bad --repo-dir, bad JSON).
EXIT_ADAPTER_ERROR = 4

_SOURCES = (task_adapter.SOURCE,)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True, choices=_SOURCES, help="the tool to ingest from")
    parser.add_argument(
        "--repo-dir",
        default="",
        dest="repo_dir",
        help="repo DIRECTORY to run the task fetch in (task resolves the project from cwd; each "
        "item is tagged with the project task reports). Named -dir to avoid task-cli's "
        "--repo, which expects owner/name, not a path.",
    )
    parser.add_argument(
        "--from-file",
        default="",
        dest="from_file",
        help="read records from a JSON file instead of shelling out",
    )
    parser.add_argument("--dry-run", action="store_true", help="preview the plan, write nothing")
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")


def run(args: argparse.Namespace) -> int:
    if args.source != task_adapter.SOURCE:  # defensive: guards a future second --source misroute
        print(f"error: unsupported source `{args.source}`", file=sys.stderr)
        return EXIT_ADAPTER_ERROR
    try:
        records = _load_records(args)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ADAPTER_ERROR
    store = open_store()
    m = machine()
    # each ticket's project comes from its own group; --repo-dir is only the fetch cwd.
    events = plan_ingest(records, load_items(store, m), m)
    new_items = sum(1 for e in events if e.kind == EventKind.INTAKE)
    if not args.dry_run:
        for event in events:
            store.append(event)
    _report(args, new_items, len(records), args.dry_run)
    return 0


def _load_records(args: argparse.Namespace) -> list[dict]:
    if args.from_file:
        import pathlib

        data = json.loads(pathlib.Path(args.from_file).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{args.from_file}: expected a JSON array of records")
        return flatten_records(data)
    return fetch_task_records(cwd=args.repo_dir)


def _report(args: argparse.Namespace, new_items: int, scanned: int, dry_run: bool) -> None:
    if args.json:
        print(
            json.dumps(
                {
                    "source": args.source,
                    "scanned": scanned,
                    "new_items": new_items,
                    "dry_run": dry_run,
                }
            )
        )
        return
    verb = "would onboard" if dry_run else "onboarded"
    print(f"{args.source}: scanned {scanned}, {verb} {new_items} new item(s)")
