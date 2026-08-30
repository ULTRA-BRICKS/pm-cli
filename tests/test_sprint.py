"""Sprint event-log fold — the sprint-side mirror of ``test_projection.py``."""

from __future__ import annotations

from pathlib import Path

from pmlib.events import EventKind, new_event
from pmlib.projection import build_projection
from pmlib.sprint import STATUS_ACTIVE, STATUS_CLOSED, STATUS_PLANNING, build_sprint_projection
from pmlib.states import default_machine
from pmlib.store import EventStore, resolve_store_dir


def test_sprint_created_with_an_empty_id_is_ignored(pm_home: Path) -> None:
    # Regression: a malformed/foreign log line with no id parses as `""` (events.py), and used to
    # be accepted as a real sprint -- a phantom `sprints[""]` reachable by every lookup, and
    # eventually surfaced as a blank-id reminder to Telegram via the cron consumer.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "", {"name": "foreign"}))
    sprints = build_sprint_projection(store.read_all())
    assert sprints == {}


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


def test_null_work_item_id_from_a_foreign_log_line_is_ignored(pm_home: Path) -> None:
    # Regression: `str(p.get("work_item_id", ""))` turned a JSON `null` into the truthy string
    # `"None"`, scoping the sprint to a phantom item -- and a lone phantom item is enough for a
    # following `sprint_started` to activate the sprint at the fold level.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": None}))
    store.append(new_event(EventKind.SPRINT_STARTED, "s1", {}))
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.item_ids() == []
    assert sprint.status == STATUS_PLANNING  # no real item ever landed, so start stays ignored


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


def test_re_add_with_a_malformed_estimate_preserves_the_prior_valid_value(pm_home: Path) -> None:
    # Regression: a malformed/foreign re-add's estimate coerces to None, which previously
    # OVERWROTE a prior valid estimate (silently corrupting the sprint total from 8 to nothing).
    # `None` from `_coerce_estimate` means "couldn't parse," not "clear it" -- unparseable input
    # must be discarded, leaving the existing valid value untouched.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": 8})
    )
    store.append(
        new_event(
            EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": "not-a-number"}
        )
    )
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.items[0].estimate == 8
    assert sprint.total_estimate() == 8


def test_two_finite_foreign_estimates_that_overflow_the_total_are_discarded(pm_home: Path) -> None:
    # Regression: the CLI's own `_add` rejects an estimate that would overflow the sprint's
    # total, but a hand-edited or foreign log line isn't bound by that -- and once inf leaks into
    # `total_estimate()` there is no remove/edit event to repair it, permanently bricking the
    # sprint's status/estimate display. The fold itself must reject the SECOND overflowing
    # estimate too, the same way the CLI does, not just tolerate the first two events blindly.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": 1e308})
    )
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w2", "estimate": 1e308})
    )
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.items[0].estimate == 1e308
    assert sprint.items[1].estimate is None  # the overflowing second estimate was discarded
    assert sprint.total_estimate() == 1e308  # never infected with inf


def test_a_python_int_too_large_for_float_does_not_crash_the_whole_read(pm_home: Path) -> None:
    # Regression: `_coerce_estimate` caught (TypeError, ValueError) but not OverflowError -- a
    # JSON-decoded integer with ~400 digits parses fine as a Python int (valid JSON, valid int)
    # but `float(that_int)` raises OverflowError, which was uncaught and crashed the WHOLE
    # `build_sprint_projection` (and therefore `pm sprint status`/`pm reminders`), not just the
    # one malformed line.
    huge_int = 10**400
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": huge_int})
    )
    sprint = build_sprint_projection(store.read_all())["s1"]  # must not raise
    assert sprint.items[0].estimate is None


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


def test_negative_estimate_from_a_foreign_log_line_is_coerced_to_none(pm_home: Path) -> None:
    # Regression: the CLI rejects a negative --estimate, but a hand-edited or foreign log line
    # isn't bound by that -- the fold's own coercion must reject it too, or a negative value
    # (valid JSON, just not a valid points/hours estimate) silently corrupts the sprint total.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": -3})
    )
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.items[0].estimate is None
    assert sprint.total_estimate() == 0


def test_boolean_estimate_from_a_foreign_log_line_is_coerced_to_none(pm_home: Path) -> None:
    # Regression: `bool` is an `int` subclass in Python, so `float(True)` silently becomes `1.0`
    # instead of the "malformed, discard" `None` a JSON boolean in a numeric field deserves.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(
        new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1", "estimate": True})
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
    store.append(new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1"}))
    store.append(new_event(EventKind.SPRINT_STARTED, "s1", {}))
    assert build_sprint_projection(store.read_all())["s1"].status == STATUS_ACTIVE


def test_start_on_an_empty_sprint_is_ignored_at_fold_level(pm_home: Path) -> None:
    # Regression: the CLI's own `_start` rejects an empty sprint ("add first"), but the fold
    # previously trusted a SPRINT_STARTED event unconditionally — a hand-edited or foreign log
    # entry isn't bound by the CLI's guard, so the fold must enforce the same invariant itself.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_STARTED, "s1", {}))
    sprint = build_sprint_projection(store.read_all())["s1"]
    assert sprint.status == STATUS_PLANNING  # the started event was ignored, not honored


def test_close_is_terminal(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1"}))
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


def test_null_retro_text_from_a_foreign_log_line_is_ignored(pm_home: Path) -> None:
    # Regression: `str(event.payload.get("text", "")).strip()` turned a JSON `null` into the
    # truthy string `"None"`, populating retro_notes and permanently (wrongly) suppressing the
    # `sprint-needs-retro` reminder for a sprint that never got a real note.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.SPRINT_CREATED, "s1", {"name": "S1"}))
    store.append(new_event(EventKind.SPRINT_ITEM_ADDED, "s1", {"work_item_id": "w1"}))
    store.append(new_event(EventKind.SPRINT_STARTED, "s1", {}))
    store.append(new_event(EventKind.SPRINT_CLOSED, "s1", {}))
    store.append(new_event(EventKind.SPRINT_RETRO, "s1", {"text": None}))
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
