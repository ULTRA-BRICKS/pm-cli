"""Reminder triggers computed purely from the projection + a clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmlib.model import DependencyEdge, TimerRef, WorkItem
from pmlib.reminders import compute_reminders
from pmlib.states import StateMachine, WorkStateCategory, WorkStateDefinition, default_machine

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _wi(iid: str, state: str = "implementing", updated: datetime | None = None,
        deps: list | None = None, timers: list | None = None) -> WorkItem:
    return WorkItem(id=iid, state=state, updated_at=(updated or NOW).isoformat(),
                    dependencies=deps or [], timers=timers or [])


def _fire(*items: WorkItem, now: datetime = NOW) -> set:
    got = compute_reminders({it.id: it for it in items}, default_machine(), now)
    return {(r.kind, r.item_id) for r in got}


def test_blocker_cleared_when_blocker_terminal() -> None:
    x = _wi("X", state="done", deps=[DependencyEdge("blocks", "Y")])  # X blocks Y, X finished
    y = _wi("Y", state="ready")
    assert ("blocker-cleared", "Y") in _fire(x, y)


def test_no_blocker_reminder_while_blocker_active() -> None:
    x = _wi("X", state="implementing", deps=[DependencyEdge("blocks", "Y")])
    y = _wi("Y", state="ready")
    assert ("blocker-cleared", "Y") not in _fire(x, y)


def test_child_done_parent_not_advanced_is_blocker_cleared() -> None:
    child = _wi("C", state="done", deps=[DependencyEdge("blocks", "P")])  # child blocks parent
    parent = _wi("P", state="ticketed")
    assert ("blocker-cleared", "P") in _fire(child, parent)


def test_wait_satisfied_when_target_terminal() -> None:
    x = _wi("X", state="ready", deps=[DependencyEdge("waits_for", "Y")])
    y = _wi("Y", state="done")
    assert ("wait-satisfied", "X") in _fire(x, y)


def test_duplicate_open_when_both_active() -> None:
    x = _wi("X", state="implementing", deps=[DependencyEdge("duplicates", "Y")])
    y = _wi("Y", state="ready")
    assert ("duplicate-open", "X") in _fire(x, y)


def test_superseded_open_when_target_active() -> None:
    x = _wi("X", state="implementing", deps=[DependencyEdge("supersedes", "Y")])
    y = _wi("Y", state="ready")
    assert ("superseded-open", "X") in _fire(x, y)


def test_relates_to_never_fires() -> None:
    x = _wi("X", state="implementing", deps=[DependencyEdge("relates_to", "Y")])
    y = _wi("Y", state="ready")
    assert not any(r for (k, r) in _fire(x, y) if r == "X")


def test_no_progress_for_stale_active_item() -> None:
    stale = _wi("S", state="implementing", updated=NOW - timedelta(days=2))
    assert ("no-progress", "S") in _fire(stale)


def test_no_progress_ignores_fresh_and_non_active() -> None:
    fresh = _wi("F", state="implementing", updated=NOW - timedelta(minutes=5))
    parked = _wi("P", state="parked", updated=NOW - timedelta(days=30))
    fired = _fire(fresh, parked)
    assert ("no-progress", "F") not in fired
    assert ("no-progress", "P") not in fired


def test_timer_expired_fires_when_due_passed() -> None:
    due = (NOW - timedelta(hours=1)).isoformat()
    item = _wi("T", state="ready", timers=[TimerRef("reminder", due, "check the deploy")])
    assert ("timer-expired", "T") in _fire(item)


def test_timer_not_expired_in_future() -> None:
    due = (NOW + timedelta(hours=1)).isoformat()
    item = _wi("T", state="ready", timers=[TimerRef("reminder", due, "later")])
    assert ("timer-expired", "T") not in _fire(item)


def test_no_progress_honors_state_sla_override() -> None:
    """A state with its own sla_seconds uses that, not the default 24h window."""
    machine = StateMachine({"busy": WorkStateDefinition(
        "busy", WorkStateCategory.ACTIVE, allowed_next=[], sla_seconds=10)}, initial_state="busy")
    item = WorkItem(id="Q", state="busy", updated_at=(NOW - timedelta(minutes=1)).isoformat())
    fired = compute_reminders({"Q": item}, machine, NOW)  # 60s > 10s SLA -> stale
    assert any(r.kind == "no-progress" and r.item_id == "Q" for r in fired)
    fresh = WorkItem(id="Q", state="busy", updated_at=(NOW - timedelta(seconds=5)).isoformat())
    assert compute_reminders({"Q": fresh}, machine, NOW) == []  # 5s < 10s SLA -> fresh


def test_dependency_cycle_detected() -> None:
    a = _wi("A", state="ready", deps=[DependencyEdge("blocks", "B")])
    b = _wi("B", state="ready", deps=[DependencyEdge("blocks", "A")])
    assert any(k == "dependency-cycle" for (k, _) in _fire(a, b))
