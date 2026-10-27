"""The task-cli adapter — project ticket records into pm work items (inbound ingest).

WHAT THIS FILE IS
    ``plan_ingest`` (PURE: task records + the current projection -> the events to append) and
    ``fetch_task_records`` (EFFECTFUL: shell out to ``task list --all --json``). The split keeps
    the mapping/dedupe logic unit-testable without task-cli on PATH. A second pure/effectful pair,
    ``extract_pr_url`` / ``fetch_ticket_attachments``, does the same for PR-url DISCOVERY (see
    ``commands/reconcile.py``'s module docstring for why bulk ingest alone can't provide it).

MAPPING & DEDUPE
    - Identity is PROJECT-QUALIFIED: a ticket becomes a WorkItem with id ``task:<ref>`` where the
      ref is ``<project>:<ticket-id>`` (or just ``<ticket-id>`` when no project is known). GitHub
      ids are per-repo, so ``#12`` in two repos are DIFFERENT tickets; without the project scope
      they would collapse into one item and the second would be silently dropped.
    - The ticket's url is captured as ``task-record`` evidence carried INLINE on the single intake
      event, so onboarding one ticket is one atomic append (no crash-window between an intake and a
      separate evidence event, which dedup would otherwise strand forever).
    - The ticket's task-cli state maps to a canonical pm state via ``INGEST_STATE_MAP`` (the
      reverse of ``task_projection`` is one-to-many, so the choice is explicit, not derived).
    - DEDUPE is by the qualified ref: a ticket already tracked by any existing WorkItem's
      ``task_refs`` produces NO events, so a re-run is a true no-op. Advancing an already-tracked
      item's pm state from later task movement is deliberately NOT done here — that reconciliation
      is a later evidence-reconciler milestone; ingest only onboards new tickets.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..events import EventKind, new_event

if TYPE_CHECKING:  # annotation-only imports (kept off the runtime import surface)
    from ..events import Event
    from ..model import WorkItem
    from ..states import StateMachine

# A GitHub pull-request URL exactly as it appears in a task-cli ticket's `attachments` (e.g. a
# Linear "GitHub PR" attachment). Anchored full-match so an unrelated github.com link (a doc, an
# issue, a commit) on the same ticket never false-positives as the tracked PR. The trailing
# `(?:[?#].*)?` tolerates a query string or fragment (e.g. `?notification_referrer_id=...`,
# `#issuecomment-...`) WITHOUT capturing it into the matched/returned url — real payloads observed
# so far are query/fragment-free, but rejecting one silently would regress a single item straight
# back to `scanned:0`, exactly the failure mode this whole feature exists to close.
_GH_PR_URL_RE = re.compile(r"^(https://github\.com/[^/\s]+/[^/\s]+/pull/\d+)/?(?:[?#].*)?$")

# The source name for this adapter (used as the intake ``source`` tag and the CLI ``--source``).
SOURCE = "task"

# task-cli state -> canonical pm state on first ingest. Explicit (the reverse projection is
# one-to-many); a state task-cli doesn't have falls back to "ticketed" (the intake landing state).
INGEST_STATE_MAP: dict[str, str] = {
    "todo": "ticketed",
    "in-progress": "implementing",
    "in-review": "reviewing",
    "done": "done",
    "cancelled": "cancelled",
}
_DEFAULT_PM_STATE = "ticketed"

# task-cli defaults to a piped limit of 30 rows PER PROJECT; without an explicit -n, ingest would
# silently truncate a busy repo to its 30 most-recent tickets. Ask for an effectively unbounded set.
_FETCH_LIMIT = "100000"

# A hung `task read` (backend network stall) must not block PR-url discovery indefinitely —
# discovery runs once per ticket lacking `pr` evidence, so one hang would otherwise stall the
# whole reconcile run. Only guards `fetch_ticket_attachments`, the new discovery call this PR
# adds; `fetch_task_records`'s existing `task list` call is untouched (working code, out of scope).
_SUBPROCESS_TIMEOUT_SECONDS = 30


def plan_ingest(
    records: Iterable[dict], existing: dict[str, WorkItem], machine: StateMachine
) -> list[Event]:
    """The events to append to onboard ``records`` — deduping tickets already tracked.

    Each record's project comes from its own group (``flatten_records`` stamps it). There is NO
    caller-supplied project override: overriding every record's project would re-collapse the
    cross-repo ``#12`` collision the qualified ref exists to prevent.
    """
    tracked = _tracked_task_ids(existing)
    events: list[Event] = []
    for rec in records:
        ticket_id = str(rec.get("id") or "")
        if not ticket_id:
            continue  # a record with no id can't be identified
        resolved_project = str(rec.get("project") or "")
        ref = _task_ref(resolved_project, ticket_id)
        if ref in tracked:
            continue  # dedupe: already onboarded (project-qualified, so cross-repo #12 don't clash)
        tracked.add(ref)  # guard against duplicate refs within the same batch
        events.append(_intake_event(rec, ref, resolved_project, machine))
    return events


def _task_ref(project: str, ticket_id: str) -> str:
    """The project-qualified dedupe/identity ref — ``<project>:<id>`` or bare ``<id>``."""
    return f"{project}:{ticket_id}" if project else ticket_id


def raw_ticket_id(ref: str) -> str:
    """The raw task-cli ticket id from a pm-qualified ref — the inverse of :func:`_task_ref`.

    Kept next to ``_task_ref`` so the ``<project>:<id>`` format can't drift between the two
    directions (a bare ref with no project has no colon and passes through unchanged).
    """
    return ref.split(":", 1)[-1]


def _tracked_task_ids(existing: dict[str, WorkItem]) -> set[str]:
    return {ref for item in existing.values() for ref in item.task_refs}


def _intake_event(rec: dict, ref: str, project: str, machine: StateMachine) -> Event:
    pm_state = INGEST_STATE_MAP.get(str(rec.get("state") or ""), _DEFAULT_PM_STATE)
    if machine.get(pm_state) is None:  # defensive: never seed an item into an unknown state
        pm_state = machine.initial_state
    payload = {
        "id": f"task:{ref}",
        "title": str(rec.get("title") or ""),
        "project": project,
        "state": pm_state,
        "labels": [str(x) for x in (rec.get("labels") or [])],
        "task_refs": [ref],
        "source": SOURCE,
    }
    url = str(rec.get("url") or "")
    if url:  # carried inline so intake+evidence is a single atomic append
        payload["evidence"] = [{"kind": "task-record", "uri": url}]
    return new_event(EventKind.INTAKE, "", payload)


def fetch_task_records(cwd: str = "") -> list[dict]:
    """Shell out to ``task list --all --json`` and return the flattened ticket records.

    ``cwd`` is the repo DIRECTORY to run ``task`` in — task-cli resolves the project from the
    working directory (its own ``--repo`` flag expects ``owner/name``, not a path), so we scope by
    cwd and let each ticket carry the project task reports. Effectful and lazy (``subprocess``
    imported here, not at module top). Raises ``RuntimeError`` with an actionable message if
    ``task`` is missing or the call fails, so the CLI renders a structured error, not a traceback.
    """
    import os
    import subprocess

    # Check the cwd first so a missing --repo-dir is reported as such, not as "task not found"
    # (subprocess raises the same FileNotFoundError for a missing executable AND a missing cwd).
    if cwd and not os.path.isdir(cwd):
        raise RuntimeError(f"--repo-dir does not exist or is not a directory: {cwd}")
    cmd = ["task", "list", "--all", "--json", "--no-pager", "-n", _FETCH_LIMIT]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=cwd or None)
    except FileNotFoundError as exc:
        raise RuntimeError("`task` CLI not found on PATH — install task-cli to ingest") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"`task list` failed (exit {proc.returncode}): {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "[]")
    if not isinstance(data, list):
        # Malformed external output follows the adapter's documented operational-error contract.
        raise RuntimeError("`task list --json` did not return a JSON array")  # noqa: TRY004
    return flatten_records(data)


def extract_pr_url(attachments: Iterable[object]) -> str:
    """The first GitHub pull-request URL among a ticket's ``attachments``, or "" if none.

    ``task list --json`` (what ``fetch_task_records`` calls for bulk ingest) omits
    ``attachments`` entirely — verified against task-cli's actual output shapes (2026-08-18):
    ``list`` tickets carry only ``{id, title, state, url, labels, what, due}``, while ``task read
    <id> --json`` additionally carries ``attachments: [{id, title, url, subtitle}, ...]``. This is
    why PR-url discovery needs a per-ticket ``task read`` (``fetch_ticket_attachments`` below), not
    the bulk list call — there is no bulk-list flag that includes attachments.

    ``attachments`` is typed ``Iterable[object]``, not ``Iterable[dict]`` — untrusted input (a
    malformed backend response) may hand back non-dict elements, and this function tolerates them.
    """
    for att in attachments:
        if not isinstance(att, dict):
            continue
        url = str(att.get("url") or "")
        m = _GH_PR_URL_RE.match(url)
        if m:
            return m.group(1)  # the canonical PR url, with any query/fragment tail stripped
    return ""


def fetch_ticket_attachments(ticket_id: str, cwd: str = "") -> list[dict]:
    """Shell out to ``task read <ticket_id> --json`` and return its ``attachments`` array.

    ``ticket_id`` is the RAW task-cli id (``HYP-1256``, ``#12``) — use :func:`raw_ticket_id` to
    strip a pm-qualified ref before calling. Effectful and lazy (``subprocess`` imported here,
    matching ``fetch_task_records``'s convention). Element-level validation of ``attachments`` is
    left to :func:`extract_pr_url` (its consumer) rather than duplicated here.

    Best-effort per ticket: a ticket that fails to read (deleted, permission, transient backend
    hiccup) returns ``[]`` rather than raising, so a discovery loop over many tickets isn't killed
    by one bad id. Raises only when ``task`` itself is missing on PATH, or ``cwd`` doesn't exist —
    the same actionable failures ``fetch_task_records`` raises, since either means NO discovery
    can work at all, not just this one ticket (``subprocess`` raises the identical
    ``FileNotFoundError`` for a missing executable AND a missing cwd, so the cwd is checked first
    to report the right one).
    """
    import os
    import subprocess

    if cwd and not os.path.isdir(cwd):
        raise RuntimeError(f"--repo-dir does not exist or is not a directory: {cwd}")
    cmd = ["task", "read", ticket_id, "--json"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd or None,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "`task` CLI not found on PATH — install task-cli to discover PR urls"
        ) from exc
    except subprocess.TimeoutExpired:
        return []  # a hung backend call must not stall discovery for every remaining ticket
    if proc.returncode != 0:
        return []  # unreadable ticket — best-effort, does not fail the whole discovery pass
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return []
    attachments = data.get("attachments") if isinstance(data, dict) else None
    return attachments if isinstance(attachments, list) else []


def flatten_records(data: list) -> list[dict]:
    """Normalize task-cli's ``list --json`` output to a flat list of ticket dicts.

    ``task list --all --json`` groups tickets BY PROJECT: ``[{project, backend, error, tickets:
    [...]}]``. This flattens each group's ``tickets`` and stamps the group's ``project`` onto each
    ticket (as a DEFAULT — a ticket that already carries its own ``project`` keeps it), and it
    passes a plain flat array (a hand-authored / piped records file) through unchanged. A group
    carrying an ``error`` is skipped (its tickets could not be listed).
    """
    if not _is_grouped(data):
        return [r for r in data if isinstance(r, dict)]
    flat: list[dict] = []
    for group in data:
        if not isinstance(group, dict) or group.get("error"):
            continue
        project = str(group.get("project", ""))
        for ticket in group.get("tickets", []) or []:
            if isinstance(ticket, dict):
                flat.append({"project": project, **ticket})  # ticket's own project (if any) wins
    return flat


def _is_grouped(data: list) -> bool:
    """Grouped output has project dicts with a ``tickets`` key and no ``id``; a flat array holds
    ticket dicts with ``id`` and no ``tickets``. Decide on the first dict element (both shapes are
    homogeneous), checking BOTH keys so a stray ``tickets`` field on a ticket can't misclassify.
    """
    first = next((el for el in data if isinstance(el, dict)), None)
    return first is not None and "tickets" in first and "id" not in first
