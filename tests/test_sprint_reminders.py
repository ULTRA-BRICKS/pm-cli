"""Sprint-lifecycle reminder triggers — pure, computed from the sprint + WorkItem projections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmlib.model import WorkItem
from pmlib.reminders import compute_sprint_reminders
from pmlib.sprint import Sprint, SprintItem
from pmlib.states import default_machine

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _fire(sprints: dict, items: dict | None = None, now: datetime = NOW) -> set:
    got = compute_sprint_reminders(sprints, items or {}, default_machine(), now)
    return {(r.kind, r.item_id) for r in got}


def test_stale_planning_fires_past_threshold() -> None:
    old = (NOW - timedelta(days=10)).isoformat()
    s = Sprint(id="s1", status="planning", created_at=old, updated_at=old)
    assert ("sprint-stale-planning", "s1") in _fire({"s1": s})


def test_stale_planning_does_not_fire_when_fresh() -> None:
    s = Sprint(id="s1", status="planning", created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    assert ("sprint-stale-planning", "s1") not in _fire({"s1": s})


def test_stale_planning_does_not_fire_once_active() -> None:
    old = (NOW - timedelta(days=10)).isoformat()
    s = Sprint(id="s1", status="active", created_at=old, updated_at=old)
    assert ("sprint-stale-planning", "s1") not in _fire({"s1": s})


def test_ready_to_close_when_all_items_terminal() -> None:
    s = Sprint(id="s1", status="active", items=[SprintItem("w1"), SprintItem("w2")])
    items = {"w1": WorkItem(id="w1", state="done"), "w2": WorkItem(id="w2", state="cancelled")}
    assert ("sprint-ready-to-close", "s1") in _fire({"s1": s}, items)


def test_not_ready_to_close_while_an_item_is_active() -> None:
    s = Sprint(id="s1", status="active", items=[SprintItem("w1"), SprintItem("w2")])
    items = {"w1": WorkItem(id="w1", state="done"), "w2": WorkItem(id="w2", state="implementing")}
    assert ("sprint-ready-to-close", "s1") not in _fire({"s1": s}, items)


def test_ready_to_close_does_not_fire_for_an_empty_sprint() -> None:
    s = Sprint(id="s1", status="active", items=[])
    assert ("sprint-ready-to-close", "s1") not in _fire({"s1": s})


def test_ready_to_close_does_not_fire_when_every_item_is_missing_from_projection() -> None:
    # Regression: `all()` over an empty (filtered-out) generator is vacuously True — a sprint
    # whose items are ALL absent from the WorkItem projection must never read as "all done".
    s = Sprint(id="s1", status="active", items=[SprintItem("ghost1"), SprintItem("ghost2")])
    assert ("sprint-ready-to-close", "s1") not in _fire({"s1": s}, {})


def test_ready_to_close_does_not_fire_when_one_item_is_missing_from_projection() -> None:
    s = Sprint(id="s1", status="active", items=[SprintItem("w1"), SprintItem("ghost")])
    items = {"w1": WorkItem(id="w1", state="done")}  # "ghost" absent entirely
    assert ("sprint-ready-to-close", "s1") not in _fire({"s1": s}, items)


def test_needs_retro_when_closed_without_notes() -> None:
    s = Sprint(id="s1", status="closed", closed_at=NOW.isoformat(), retro_notes=[])
    assert ("sprint-needs-retro", "s1") in _fire({"s1": s})


def test_no_retro_reminder_once_a_note_exists() -> None:
    s = Sprint(id="s1", status="closed", closed_at=NOW.isoformat(), retro_notes=["went fine"])
    assert ("sprint-needs-retro", "s1") not in _fire({"s1": s})


def test_no_retro_reminder_while_still_active() -> None:
    s = Sprint(id="s1", status="active", retro_notes=[])
    assert ("sprint-needs-retro", "s1") not in _fire({"s1": s})
