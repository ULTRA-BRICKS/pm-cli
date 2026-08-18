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

PR-URL DISCOVERY (the ``fetch_task_records``/``pr_url_for`` gap)
    ``fetch_pr_facts`` only finds a PR for an item that already carries ``pr`` evidence — and
    nothing ever wrote that evidence: ``pm ingest --source task`` records ONLY ``task-record``
    evidence (the ticket's own url), because ``task list --json`` (what bulk ingest calls) omits
    the ``attachments`` field where a ticket's linked PR actually lives; only ``task read <id>``
    per-ticket carries it. Left alone this made ``pm reconcile --source pr`` return ``scanned:0``
    forever, for every item, task-ingested or not. ``_discover_pr_urls`` closes that gap HERE
    (not at ingest time) by shelling ``task read`` for any item still missing ``pr`` evidence —
    which backfills items ingested before this fix existed too, not just future ones.

    SCOPE (deliberately not addressed here, tracked as follow-ups):
    - Single ``--repo-dir`` == single task-cli project, exactly like ``pm ingest --source task``
      is already scoped per call. A store holding items from MULTIPLE projects needs one
      ``reconcile --source pr --repo-dir <that project's dir>`` call per project — passing the
      wrong repo-dir for a GitHub-issues item risks resolving a same-numbered issue in the wrong
      repo. Not a regression (ingest has the identical scope), but worth a real fix before this
      runs against more than one project at once. ``_discover_pr_urls``'s per-ticket-id memo
      (``read_by_ticket``) inherits the same assumption: it's keyed on the RAW id (project prefix
      stripped), so a same-numbered ticket in a second project sharing this run would silently
      reuse the first project's ``task read`` result. Consistent with the single-project scope
      above, but worth keying by the qualified ``ref`` instead if multi-project support lands.
    - No negative-caching: an item whose ticket genuinely has no PR attachment is re-probed via
      `task read` on every future run (not just once) — deliberately not cached, since a durable
      "no PR" marker could go stale the moment someone attaches one later, and the measured cost
      (~100 tickets, well under a couple of minutes) doesn't yet justify staleness-aware caching.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

from ..adapters import task as task_adapter
from ..adapters.github import fetch_pr_facts, pr_url_for
from ..adapters.rig_health import HealthStatus, check_rig_health, interpret_rig_status
from ..events import EventKind
from ..reconcile import plan_pr_reconcile
from ._shared import load_items, machine, open_store

if TYPE_CHECKING:  # annotation-only imports (kept off the runtime import surface)
    from ..model import WorkItem

NAME = "reconcile"
HELP = "advance work items on unforgeable evidence (--source pr|rig)"

EXIT_ADAPTER_ERROR = 4
_SOURCES = ("pr", "rig")


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True, choices=_SOURCES, help="the evidence source")
    parser.add_argument(
        "--repo-dir",
        default="",
        dest="repo_dir",
        help="repo dir to check (rig: `rig status` cwd; pr: `task read` cwd for PR-url discovery)",
    )
    parser.add_argument(
        "--from-file",
        default="",
        dest="from_file",
        help="pre-fetched facts (pr: array; rig: {returncode,stdout,stderr})",
    )
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
    if args.from_file:
        facts = _pr_facts_from_file(args)
    else:
        discovered = _discover_pr_urls(items, args.repo_dir)
        facts = fetch_pr_facts(items, discovered)
    events = plan_pr_reconcile(items, facts, m)
    transitions = sum(1 for e in events if e.kind == EventKind.TRANSITION)
    if not args.dry_run:
        for event in events:
            store.append(event)
    _report_pr(
        args, len(facts), transitions, sum(1 for e in events if e.kind == EventKind.EVIDENCE)
    )
    return 0


def _discover_pr_urls(items: dict[str, WorkItem], repo_dir: str) -> dict[str, str]:
    """PR urls found via ``task read`` for items that don't already carry ``pr`` evidence.

    See the module docstring's "PR-URL DISCOVERY" section for why this exists. Best-effort: a
    ticket that can't be read, or carries no GitHub PR attachment, is simply skipped — it stays
    unresolved for this run rather than failing the whole reconcile.

    Tries EVERY ``task_ref`` on the item, not just the first — an item can aggregate more than
    one tracked ticket, and the PR-carrying one isn't guaranteed to be ``task_refs[0]``.

    A missing ``task`` binary (or an invalid ``repo_dir``) makes ``fetch_ticket_attachments``
    raise ``RuntimeError`` — categorically, for every remaining item, not just this one. Catching
    it here and returning whatever was already discovered (instead of letting it propagate) keeps
    that failure scoped to "discovery didn't run this time": items that already carry ``pr``
    evidence, and never needed ``task`` at all, still get reconciled via ``gh`` alone. Before this
    guard, one missing-``task`` item aborted the ENTIRE reconcile run — a real regression, since
    ``reconcile --source pr`` depended on ``gh`` only before PR-url discovery was added.

    ``ticket_id`` is memoized within this call: a ticket reachable from more than one item (a
    shared ``task_ref``) is ``task read`` ONCE, mirroring ``fetch_pr_facts``'s ``by_url`` memo.
    """
    discovered: dict[str, str] = {}
    read_by_ticket: dict[str, list[dict]] = {}
    for item_id, item in items.items():
        if pr_url_for(item) or not item.task_refs:
            continue
        for ref in item.task_refs:
            ticket_id = task_adapter.raw_ticket_id(ref)
            if ticket_id not in read_by_ticket:
                try:
                    read_by_ticket[ticket_id] = task_adapter.fetch_ticket_attachments(
                        ticket_id, cwd=repo_dir
                    )
                except RuntimeError as exc:
                    # `task` itself is unusable (missing binary / bad --repo-dir) — categorical
                    # for every remaining item, not just this one. Stop probing rather than
                    # aborting the whole reconcile, but say so: a silent partial `scanned:N` here
                    # is exactly the invisible-failure mode this feature exists to close.
                    print(f"reconcile: PR-url discovery skipped — {exc}", file=sys.stderr)
                    return discovered
            url = task_adapter.extract_pr_url(read_by_ticket[ticket_id])
            if url:
                discovered[item_id] = url
                break
    return discovered


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
    return interpret_rig_status(
        int(data.get("returncode", 0)), str(data.get("stdout", "")), str(data.get("stderr", ""))
    )


def _report_pr(args: argparse.Namespace, scanned: int, transitions: int, evidence: int) -> None:
    if args.json:
        print(
            json.dumps(
                {
                    "source": "pr",
                    "scanned": scanned,
                    "transitions": transitions,
                    "evidence": evidence,
                    "dry_run": args.dry_run,
                }
            )
        )
        return
    verb = "would advance" if args.dry_run else "advanced"
    print(f"pr: scanned {scanned}, {verb} {transitions} transition(s), +{evidence} evidence")
