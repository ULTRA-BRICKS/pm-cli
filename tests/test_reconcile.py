"""The PR/CI reconcile engine — advances ONLY on unforgeable facts, stops before deploy/done."""

from __future__ import annotations

from pmlib.events import EventKind
from pmlib.model import EvidenceRef, WorkItem
from pmlib.reconcile import plan_pr_reconcile
from pmlib.states import default_machine

_URL = "https://github.com/x/y/pull/1"


def _item(iid: str, state: str, evidence: list | None = None) -> WorkItem:
    return WorkItem(id=iid, state=state, evidence=evidence or [])


def _targets(events: list) -> list[str]:
    return [e.payload["to"] for e in events if e.kind == EventKind.TRANSITION]


def _ev_kinds(events: list) -> list[tuple]:
    return [(e.payload["kind"], e.payload["digest"]) for e in events if e.kind == EventKind.EVIDENCE]


def _reconcile(item: WorkItem, fact: dict) -> list:
    return plan_pr_reconcile({item.id: item}, {item.id: fact}, default_machine())


def test_green_ci_advances_pr_open_to_ship_ready() -> None:
    events = _reconcile(_item("w", "pr-open"),
                        {"pr_url": _URL, "pr_state": "open", "ci": "success"})
    assert _targets(events) == ["ci-pending", "ship-ready"]  # steps through the FSM, no skips
    assert ("ci", "success") in _ev_kinds(events)


def test_red_ci_records_evidence_but_does_not_transition() -> None:
    """Red CI is recorded as evidence; the engine never auto-moves the item (no stranding)."""
    events = _reconcile(_item("w", "ci-pending"),
                        {"pr_url": _URL, "pr_state": "open", "ci": "failure"})
    assert _targets(events) == []  # no auto-transition to fixing
    assert ("ci", "failure") in _ev_kinds(events)


def test_pending_ci_advances_pr_open_to_ci_pending() -> None:
    events = _reconcile(_item("w", "pr-open"),
                        {"pr_url": _URL, "pr_state": "open", "ci": "pending"})
    assert _targets(events) == ["ci-pending"]


def test_mid_window_green_advances_to_ship_ready() -> None:
    events = _reconcile(_item("w", "ci-pending"),
                        {"pr_url": _URL, "pr_state": "open", "ci": "success"})
    assert _targets(events) == ["ship-ready"]


def test_no_checks_yet_does_not_advance() -> None:
    events = _reconcile(_item("w", "pr-open"),
                        {"pr_url": _URL, "pr_state": "open", "ci": "none"})
    assert _targets(events) == []


def test_merged_pr_records_evidence_but_never_advances_past_ship_ready() -> None:
    """The fork boundary: a merged PR is unforgeable evidence, but advancing to deployed/done needs
    the deploy-probe (fork b) / ship-audit (fork a) decision — the reconciler must NOT do it."""
    events = _reconcile(_item("w", "ship-ready"),
                        {"pr_url": _URL, "pr_state": "merged", "ci": "success"})
    assert _targets(events) == []  # no deployed/done transition
    assert ("pr", "merged") in _ev_kinds(events)  # ...but the merge fact is recorded


def test_reconcile_is_idempotent_at_convergence() -> None:
    item = _item("w", "ship-ready", evidence=[
        EvidenceRef("pr", _URL, "t", "merged"), EvidenceRef("ci", _URL, "t", "success")])
    events = _reconcile(item, {"pr_url": _URL, "pr_state": "merged", "ci": "success"})
    assert events == []  # nothing to add — evidence present, state already at the window top


def test_unknown_item_in_facts_is_skipped() -> None:
    events = plan_pr_reconcile({}, {"ghost": {"pr_url": _URL, "ci": "success"}}, default_machine())
    assert events == []
