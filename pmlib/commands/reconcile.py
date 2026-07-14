"""``pm reconcile`` — advance work items on unforgeable external evidence.

Two sources:
- ``pr``: read each item's PR state + CI conclusion (via ``gh``) and advance it along the CI window
  (``pr-open -> ci-pending -> ship-ready`` on green, ``-> fixing`` on red), attaching pr/ci evidence
  idempotently. It NEVER advances past ship-ready — ``deployed`` / ``done`` are behind the
  deploy-probe and ship-audit decisions (research #255 forks a/b), deliberately not crossed here.
- ``rig``: run ``rig status`` and report a first-class ``config-drift`` health status when the
  config/schema skews, instead of a silent failure.

``--from-file`` supplies pre-fetched facts (pr: an array of fact records; rig: a
``{returncode, stdout, stderr}`` object) so the flow is testable without ``gh`` / ``rig``.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..adapters.github import fetch_pr_facts
from ..adapters.rig_health import HealthStatus, check_rig_health, interpret_rig_status
from ..events import EventKind
from ..reconcile import plan_pr_reconcile
from ._shared import load_items, machine, open_store

NAME = "reconcile"
HELP = "advance work items on unforgeable evidence (--source pr|rig)"

EXIT_ADAPTER_ERROR = 4
_SOURCES = ("pr", "rig")


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True, choices=_SOURCES, help="the evidence source")
    parser.add_argument("--repo-dir", default="", dest="repo_dir", help="(rig) repo dir to check")
    parser.add_argument("--from-file", default="", dest="from_file",
                        help="pre-fetched facts (pr: array; rig: {returncode,stdout,stderr})")
    parser.add_argument("--dry-run", action="store_true", help="preview the plan, write nothing")
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")


def run(args: argparse.Namespace) -> int:
    try:
        return _run_pr(args) if args.source == "pr" else _run_rig(args)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ADAPTER_ERROR


def _run_pr(args: argparse.Namespace) -> int:
    store = open_store()
    m = machine()
    items = load_items(store, m)
    facts = _pr_facts_from_file(args) if args.from_file else fetch_pr_facts(items)
    events = plan_pr_reconcile(items, facts, m)
    transitions = sum(1 for e in events if e.kind == EventKind.TRANSITION)
    if not args.dry_run:
        for event in events:
            store.append(event)
    _report_pr(args, len(facts), transitions,
               sum(1 for e in events if e.kind == EventKind.EVIDENCE))
    return 0


def _run_rig(args: argparse.Namespace) -> int:
    health = _rig_health_from_file(args) if args.from_file else check_rig_health(args.repo_dir)
    if args.json:
        print(json.dumps(health.to_dict()))
    else:
        print(f"rig: {health.status}" + (f" — {health.detail}" if health.detail else ""))
    return 0


def _pr_facts_from_file(args: argparse.Namespace) -> dict[str, dict]:
    import pathlib

    data = json.loads(pathlib.Path(args.from_file).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{args.from_file}: expected a JSON array of fact records")
    return {str(r["item_id"]): r for r in data if isinstance(r, dict) and r.get("item_id")}


def _rig_health_from_file(args: argparse.Namespace) -> HealthStatus:
    import pathlib

    data = json.loads(pathlib.Path(args.from_file).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{args.from_file}: expected a JSON object")
    return interpret_rig_status(int(data.get("returncode", 0)),
                                str(data.get("stdout", "")), str(data.get("stderr", "")))


def _report_pr(args: argparse.Namespace, scanned: int, transitions: int, evidence: int) -> None:
    if args.json:
        print(json.dumps({"source": "pr", "scanned": scanned, "transitions": transitions,
                          "evidence": evidence, "dry_run": args.dry_run}))
        return
    verb = "would advance" if args.dry_run else "advanced"
    print(f"pr: scanned {scanned}, {verb} {transitions} transition(s), +{evidence} evidence")
