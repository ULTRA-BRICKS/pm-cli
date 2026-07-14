"""The PR/CI reconcile engine — advance work items ONLY on unforgeable facts.

WHAT THIS FILE IS
    ``plan_pr_reconcile`` (PURE: work items + fetched GitHub facts -> the events to append). It
    attaches ``pr`` / ``ci`` evidence idempotently and advances state along the CI window using only
    facts the PM agent cannot forge (GitHub PR state + CI conclusion).

THE FORK BOUNDARY (research #255, deliberately NOT crossed here)
    The reconciler advances at most to ``ship-ready`` on green CI. It records a merged PR as
    evidence but NEVER advances to ``deployed`` / ``done`` — those terminal delivery states need
    (a) ship-audit as a trusted authority (needs tamper-evidence/path-hardening) and (b) a
    per-project deploy probe. Both are open decisions; this engine stops exactly at that line.

INVARIANTS
    - Pure + deterministic. Idempotent at convergence: a re-run with the same facts appends nothing
      (evidence is deduped by (kind, uri, digest); a transition fires only when the state changes).
    - Only CI-window advances happen: ``pr-open -> ci-pending -> ship-ready`` on green,
      ``ci-pending -> fixing`` on red. No skips (each hop is a legal FSM transition), no terminals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .events import Event, EventKind, new_event
from .states import EvidenceError, TransitionError

if TYPE_CHECKING:  # annotation-only
    from .model import WorkItem
    from .states import StateMachine

# The CI happy-path window this engine is allowed to advance FORWARD through (in order). Red CI is
# recorded as evidence only — the engine never auto-moves an item to ``fixing`` (that is a workflow
# decision, and auto-moving would strand the item there since ``fixing`` is off this path).
_CI_HAPPY_PATH = ("pr-open", "ci-pending", "ship-ready")


def plan_pr_reconcile(items: dict[str, WorkItem], facts: dict[str, dict],
                      machine: StateMachine) -> list[Event]:
    """Events to append so each item reflects its unforgeable PR/CI facts (idempotent)."""
    events: list[Event] = []
    for item_id, fact in facts.items():
        item = items.get(item_id)
        if item is None:
            continue  # a fact for an unknown item — skip
        events.extend(_evidence_events(item, fact))
        events.extend(_advance_events(item, fact, machine))
    return events


def _evidence_events(item: WorkItem, fact: dict) -> list[Event]:
    """New ``pr`` / ``ci`` evidence for facts not already recorded (dedup by kind+uri+digest)."""
    have = {(e.kind, e.uri, e.digest) for e in item.evidence}
    pr_url = str(fact.get("pr_url") or "")
    out: list[Event] = []
    pr_state = str(fact.get("pr_state") or "")
    if pr_url and pr_state and ("pr", pr_url, pr_state) not in have:
        out.append(_evidence(item.id, "pr", pr_url, pr_state))
    ci = str(fact.get("ci") or "")
    if pr_url and ci in ("success", "failure") and ("ci", pr_url, ci) not in have:
        out.append(_evidence(item.id, "ci", pr_url, ci))
    return out


def _evidence(item_id: str, kind: str, uri: str, digest: str) -> Event:
    return new_event(EventKind.EVIDENCE, item_id, {"kind": kind, "uri": uri, "digest": digest})


def _advance_events(item: WorkItem, fact: dict, machine: StateMachine) -> list[Event]:
    """Forward CI-window transitions implied by the CI conclusion, EACH validated against the FSM.

    The path is validated hop-by-hop against ``machine`` (not just the hardcoded window) so a future
    FSM change can never make the reconciler append an illegal transition — it stops at the first
    hop the machine rejects.
    """
    events: list[Event] = []
    state = item.state
    evidence_kinds = item.evidence_kinds()
    for target in _advance_path(state, str(fact.get("ci") or "")):
        try:
            machine.validate_transition(state, target, evidence_kinds=evidence_kinds, force=False)
        except (TransitionError, EvidenceError):
            break  # FSM diverged from the window — stop rather than append an illegal move
        events.append(new_event(EventKind.TRANSITION, item.id, {"to": target}))
        state = target
    return events


def _advance_path(current: str, ci: str) -> list[str]:
    """States to move ``current`` FORWARD through given the CI conclusion (green/pending only).

    Red CI returns [] — the engine records the ``ci=failure`` evidence but leaves the state for the
    workflow to act on, so an item is never auto-stranded in ``fixing``.
    """
    if ci not in ("success", "pending") or current not in _CI_HAPPY_PATH:
        return []
    target = "ship-ready" if ci == "success" else "ci-pending"
    here, there = _CI_HAPPY_PATH.index(current), _CI_HAPPY_PATH.index(target)
    return list(_CI_HAPPY_PATH[here + 1:there + 1]) if there > here else []
