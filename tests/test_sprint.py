"""Sprint event-log fold — the sprint-side mirror of ``test_projection.py``."""

from __future__ import annotations

from pathlib import Path

from pmlib.events import EventKind, new_event
from pmlib.projection import build_projection
from pmlib.sprint import STATUS_ACTIVE, STATUS_CLOSED, STATUS_PLANNING, build_sprint_projection
from pmlib.states import default_machine
from pmlib.store import EventStore, resolve_store_dir


def test_sprint_created_starts_in_planning(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "Sprint 1", "goal": "ship X"}))
    sprints = build_sprint_projection(store.read_all())
    assert sprints["s1"].status == STATUS_PLANNING
    assert sprints["s1"].name == "Sprint 1"
    assert sprints["s1"].goal == "ship X"


def test_add_item_records_estimate(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": 3.0})
    )
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.item_ids() == ["w1"]
    assert sprint.total_estimate() == 3.0


def test_re_add_same_item_re_estimates_instead_of_duplicating(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": 3})
    )
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": 5})
    )
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert len(sprint.items) == 1
    assert sprint.items[0].estimate == 5


def test_re_add_without_estimate_key_does_not_wipe_prior_estimate(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": 3})
    )
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1"})
    )  # bare re-add
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.items[0].estimate == 3  # untouched, not wiped to None


def test_non_finite_estimate_is_coerced_to_none(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(
            EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": float("nan")}
        )
    )
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.items[0].estimate is None


def test_add_item_after_start_is_ignored_scope_is_frozen(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1"}))
    store.append(new_event(EventKind.SPRINT_STARTED, "s1", {}))
    store.append(new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w2"}))
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.item_ids() == ["w1"]  # w2 never got in — scope froze at start


def test_start_moves_planning_to_active(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_STARTED, "s1", {}))
    assert build_sprint_projection(store.read_all())["s1"].status == STATUS_ACTIVE


def test_close_is_terminal(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_STARTED, "s1", {}))
    store.append(new_event(EventKind.SPRINT_CLOSED, "s1", {}))
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.status == STATUS_CLOSED
    assert sprint.closed_at


def test_close_from_planning_is_ignored_at_fold_level(pm_home: Path) -> None:
    # A foreign writer / hand-edited log line closing a never-started sprint must not succeed —
    # mirrors _apply_started's planning-only guard, and matches the CLI's own rejection.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_CLOSED, "s1", {}))
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.status == STATUS_PLANNING


def test_retro_note_accumulates_once_closed(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1"}))
    store.append(new_event(EventKind.SPRINT_STARTED, "s1", {}))
    store.append(new_event(EventKind.SPRINT_CLOSED, "s1", {}))
    store.append(new_event(EventKind.SPRINT_RETRO, "s1", {"text": "shipped on time"}))
    store.append(new_event(EventKind.SPRINT_RETRO, "s1", {"text": "estimates were low"}))
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.retro_notes == ["shipped on time", "estimates were low"]


def test_retro_note_before_close_is_ignored(pm_home: Path) -> None:
    # A note recorded during planning/active must NOT count — it would permanently satisfy
    # `sprint-needs-retro`, whose point is catching a skipped POST-close retro.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_RETRO, "s1", {"text": "too early"}))
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.retro_notes == []


def test_events_for_unknown_sprint_are_ignored(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_ITEM_ADDED, "ghost", {"work_item_id": "w1"}))
    store.append(new_event(EventKind.SPRINT_STARTED, "ghost", {}))
    assert build_sprint_projection(store.read_all()) == {}


def test_duplicate_create_keeps_the_first(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "First"}))
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "Second"}))
    assert build_sprint_projection(store.read_all())["s1"].name == "First"


def test_workitem_events_do_not_pollute_the_sprint_fold(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.INTAKE, "", {"id": "w1", "title": "unrelated"}))
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    sprints = build_sprint_projection(store.read_all())
    assert list(sprints.keys()) == ["s1"]


def test_sprint_events_do_not_pollute_the_workitem_projection(pm_home: Path) -> None:
    # The reverse of the test above: sprint events reuse `item_id` for the sprint id (e.g.
    # "s1"), so build_projection must ignore them rather than creating a phantom WorkItem.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1"}))
    store.append(new_event(EventKind.SPRINT_STARTED, "s1", {}))
    store.append(new_event(EventKind.INTAKE, "", {"id": "w1", "title": "real work item"}))
    items = build_projection(store.read_all(), default_machine())
    assert list(items.keys()) == ["w1"]


def test_malformed_estimate_is_lenient_not_crashing(pm_home: Path) -> None:
    # Mirrors build_projection's lenient-replay invariant: one bad hand-edited/foreign line must
    # never crash the whole fold.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(
            EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": "not-a-number"}
        )
    )
    sprint = build_sprint_projection(store.read_all())["s1"]  # must not raise
    assert sprint.items[0].estimate is None
