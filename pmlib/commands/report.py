"""``pm report <kind>`` — the read-only reporting surface (queue / stuck / retro / monthly / metrics / aging).

Renders a report from the projection + the event log; every number is traceable to the item ids
behind it. Read-only: writes no events, dispatches nothing. ``--month`` scopes the monthly report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from ..projection import build_intake_ledger
from ..reports import Report, aging_report, metrics_report, monthly_report, queue_report, retro_report, stuck_report
from ._shared import load_items, machine, open_store

NAME = "report"
HELP = "render a report (queue | stuck | retro | monthly | metrics | aging)"

_KINDS = ("queue", "stuck", "retro", "monthly", "metrics", "aging")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")  # YYYY-MM with a real month 01-12


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("kind", choices=_KINDS, help="which report to render")
    parser.add_argument("--month", default="", help="(monthly) the month YYYY-MM (default: current)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")


def run(args: argparse.Namespace) -> int:
    if args.kind == "monthly" and args.month and not _MONTH_RE.match(args.month):
        print(f"error: --month must be YYYY-MM (got {args.month!r})", file=sys.stderr)
        return 2
    store = open_store()
    m = machine()
    events = store.read_all()
    items = load_items(store, m)
    now = datetime.now(timezone.utc)
    report = _build(args, events, items, m, now)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False))
    else:
        _print_text(report)
    return 0


def _build(args: argparse.Namespace, events: list, items: dict, m, now: datetime) -> Report:
    if args.kind == "queue":
        return queue_report(items, m)
    if args.kind == "stuck":
        return stuck_report(items, m, now, build_intake_ledger(events))
    if args.kind == "retro":
        return retro_report(items, m, now)
    if args.kind == "metrics":
        return metrics_report(items, m, now)
    if args.kind == "aging":
        return aging_report(items, m, now)
    month = args.month or now.strftime("%Y-%m")
    return monthly_report(events, items, m, month)


def _print_text(report: Report) -> None:
    header = f"# {report.kind} report"
    if report.scope:
        header += f"  ({report.scope})"
    print(header)
    if report.kind == "queue":
        print(f"  total: {report.total}")
    for group, sections in report.groups.items():
        print(f"\n[{group}]")
        for s in sections:
            if report.kind == "metrics" and s.mean_seconds is not None:
                print(
                    f"  {s.label:<16} n={s.count:<4}  "
                    f"mean={_format_seconds(s.mean_seconds)}  "
                    f"p50={_format_seconds(s.p50_seconds or 0)}  "
                    f"p90={_format_seconds(s.p90_seconds or 0)}  "
                    f"{', '.join(s.item_ids[:8])}"
                )
            else:
                print(f"  {s.label:<16} {s.count:>4}   {', '.join(s.item_ids[:8])}")
    for entry in report.entries:
        print(f"  - {_entry_line(entry)}")
    for pat in report.patterns:
        print(f"  ~ {pat['state']:<20} x{pat['count']}  {', '.join(pat['item_ids'])}")


def _entry_line(entry: dict) -> str:
    if "age_seconds" in entry:  # an aging row
        return (
            f"{entry['item_id']:<12} {entry.get('state', ''):<22} "
            f"age={_format_seconds(entry['age_seconds'])}  "
            f"threshold={_format_seconds(entry['threshold_seconds'])}"
        )
    if "trigger_state" in entry:  # a retro fact
        status = "recovered" if entry.get("recovered") else "UNRESOLVED"
        secs = entry.get("time_in_problem_seconds")
        dur = f" ({secs}s)" if secs is not None else ""
        return f"{entry['item_id']:<12} {entry['trigger_state']:<22} {status}{dur}"
    return f"{entry.get('kind', ''):<14} {entry['item_id']}  {entry.get('detail', '')}"


def _format_seconds(seconds: int) -> str:
    """Human duration for text reports; JSON keeps raw seconds."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"
