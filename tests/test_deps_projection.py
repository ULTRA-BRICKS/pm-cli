"""Folding LINK / TIMER events into WorkItem dependencies and timers."""

from __future__ import annotations

from pathlib import Path

from pmlib.events import EventKind, new_event
from pmlib.projection import build_projection
from pmlib.states import default_machine
from pmlib.store import EventStore, resolve_store_dir


def _seed(store: EventStore) -> None:
    store.append(new_event(EventKind.INTAKE, "", {"id": "a", "title": "A"}))
    store.append(new_event(EventKind.INTAKE, "", {"id": "b", "title": "B"}))


def test_link_event_appends_dependency_edge(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    _seed(store)
    store.append(new_event(EventKind.LINK, "a",
                           {"type": "blocks", "target_id": "b", "reason": "schema first"}))
    items = build_projection(store.read_all(), default_machine())
    edge = items["a"].dependencies[0]
    assert edge.edge_type == "blocks"
    assert edge.target_id == "b"
    assert edge.reason == "schema first"


def test_timer_event_appends_timer(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    _seed(store)
    store.append(new_event(EventKind.TIMER, "a",
                           {"kind": "reminder", "due_at": "2026-07-20T00:00:00Z", "reason": "ping"}))
    items = build_projection(store.read_all(), default_machine())
    timer = items["a"].timers[0]
    assert timer.kind == "reminder"
    assert timer.due_at == "2026-07-20T00:00:00Z"


def test_link_for_unknown_item_is_ignored(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.LINK, "ghost", {"type": "blocks", "target_id": "b"}))
    assert build_projection(store.read_all(), default_machine()) == {}


def test_condition_survives_to_json(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    _seed(store)
    cond = {"provider": "ci", "subject": "build", "expected": "green"}
    store.append(new_event(EventKind.LINK, "a",
                           {"type": "waits_for", "target_id": "b", "condition": cond}))
    items = build_projection(store.read_all(), default_machine())
    assert items["a"].dependencies[0].condition == cond
    assert items["a"].to_dict()["dependencies"][0]["condition"] == cond


def test_malformed_condition_does_not_crash_replay(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    _seed(store)
    store.append(new_event(EventKind.LINK, "a",
                           {"type": "blocks", "target_id": "b", "condition": "not-a-mapping"}))
    items = build_projection(store.read_all(), default_machine())  # must not raise
    assert items["a"].dependencies[0].condition == {}


def test_deps_replay_is_deterministic(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    _seed(store)
    store.append(new_event(EventKind.LINK, "a", {"type": "waits_for", "target_id": "b"}))
    first = build_projection(store.read_all(), default_machine())
    second = build_projection(EventStore(resolve_store_dir()).read_all(), default_machine())
    assert first == second
