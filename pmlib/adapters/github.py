"""The GitHub adapter — find an item's PR and read its unforgeable state + CI conclusion.

``pr_url_for`` (PURE) locates the PR an item is tracking (its most-recent ``pr`` evidence).
``parse_pr_facts`` (PURE) turns ``gh pr view --json state,statusCheckRollup`` output into a
reconcile fact. ``fetch_pr_facts`` (EFFECTFUL) shells out to ``gh`` for every item with a PR.
GitHub is the authority: a PR's merge state and CI conclusion are facts the PM agent cannot forge.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only
    from ..model import WorkItem

_STATE_MAP = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


def pr_url_for(item: WorkItem) -> str:
    """The PR url the item tracks — its most-recent ``pr`` evidence, or "" if none."""
    for ev in reversed(item.evidence):
        if ev.kind == "pr" and ev.uri:
            return ev.uri
    return ""


def parse_pr_facts(pr_url: str, gh: dict) -> dict:
    """A reconcile fact ``{pr_url, pr_state, ci}`` from one ``gh pr view --json ...`` object."""
    raw_state = str(gh.get("state", "")).upper()
    # An unrecognized state is surfaced (lowercased) rather than masked as "open".
    pr_state = _STATE_MAP.get(raw_state, raw_state.lower() or "open")
    return {"pr_url": pr_url, "pr_state": pr_state, "ci": _ci_conclusion(gh.get("statusCheckRollup"))}


def _ci_conclusion(rollup: object) -> str:
    """Aggregate a statusCheckRollup into success | failure | pending | none."""
    if not isinstance(rollup, list) or not rollup:
        return "none"
    conclusions = [str((c or {}).get("conclusion") or "").upper() for c in rollup
                   if isinstance(c, dict)]
    if not conclusions:
        return "pending"  # a non-empty rollup we can't read is NOT a green — never a false success
    if any(c in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") for c in conclusions):
        return "failure"
    if any(c not in ("SUCCESS", "NEUTRAL", "SKIPPED") for c in conclusions):
        return "pending"  # something has no terminal conclusion yet
    return "success"


def fetch_pr_facts(items: dict[str, WorkItem]) -> dict[str, dict]:
    """Shell out to ``gh pr view`` for every item that tracks a PR. Effectful and lazy.

    Raises ``RuntimeError`` (actionable) if ``gh`` is missing. An individual PR that fails to read
    is skipped (best-effort) rather than failing the whole reconcile.
    """
    import subprocess

    facts: dict[str, dict] = {}
    for item_id, item in items.items():
        pr_url = pr_url_for(item)
        if not pr_url:
            continue
        try:
            proc = subprocess.run(
                ["gh", "pr", "view", pr_url, "--json", "state,statusCheckRollup"],
                capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError("`gh` CLI not found on PATH — install GitHub CLI to reconcile PRs") from exc
        if proc.returncode != 0:
            continue  # best-effort: skip a PR we can't read
        try:
            facts[item_id] = parse_pr_facts(pr_url, json.loads(proc.stdout or "{}"))
        except (ValueError, TypeError):
            continue
    return facts
