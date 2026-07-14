"""``pm dlq`` — show the intake dead-letter queue (the raw-request ledger).

Read-only. Lists every captured inbound request and its processing status
(unprocessed / processed / deadletter), with a JSON mode and a ``--status`` filter. This is the
"organ of sight" surface for the DLQ: what came in, what became a WorkItem, and what repeatedly
failed to process (dead-lettered) so it is visible instead of silently lost.
"""

from __future__ import annotations

import argparse
import json

from ..intake import STATUS_DEADLETTER, STATUS_PROCESSED, STATUS_UNPROCESSED
from ..projection import build_intake_ledger
from ._shared import open_store

NAME = "dlq"
HELP = "show the intake dead-letter queue (captured inbound requests + status)"

_STATUSES = (STATUS_UNPROCESSED, STATUS_PROCESSED, STATUS_DEADLETTER)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit a JSON array instead of a table")
    parser.add_argument("--status", default="", choices=("", *_STATUSES),
                        help="only intakes in this status")


def run(args: argparse.Namespace) -> int:
    ledger = build_intake_ledger(open_store().read_all())
    intakes = [i for i in ledger.values() if not args.status or i.status == args.status]
    intakes.sort(key=lambda i: (i.captured_at, i.id))
    if args.json:
        print(json.dumps([i.to_dict() for i in intakes], ensure_ascii=False))
        return 0
    _print_table(intakes)
    return 0


def _print_table(intakes: list) -> None:
    if not intakes:
        print("(dlq empty)")
        return
    counts = _counts(intakes)
    id_w = max((len(i.id) for i in intakes), default=6)
    for i in intakes:
        att = f" x{i.attempts}" if i.attempts else ""
        print(f"{i.id:<{id_w}}  {i.status:<11}{att:<4}  {i.sender}: {i.text[:50]}")
    print(f"-- {counts} --")


def _counts(intakes: list) -> str:
    tally: dict[str, int] = {}
    for i in intakes:
        tally[i.status] = tally.get(i.status, 0) + 1
    return ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())) or "empty"
