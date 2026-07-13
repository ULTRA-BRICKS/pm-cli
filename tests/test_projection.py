"""Replaying the event log into WorkItems — the crash/restart determinism invariant."""

from __future__ import annotations

from pathlib import Path

from pmlib.events import EventKind, new_event
from pmlib.projection import build_projection
from pmlib.states import default_machine
from pmlib.store import EventStore, resolve_store_dir


def _seed(store: EventStore) -> str:
    ev = store.append(
        new_event(EventKind.INTAKE, "", {"id": "w1", "title": "Fix login", "project": "app"})
    )
    store.append(new_event(EventKind.TRANSITION, "w1", {"to": "classified"}))
    store.append(new_event(EventKind.EVIDENCE, "w1", {"kind": "task-record", "uri": "#12"}))
    return ev.id


def test_intake_event_creates_item(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.INTAKE, "", {"id": "w1", "title": "Fix login"}))
    items = build_projection(store.read_all(), default_machine())
    assert "w1" in items
    assert items["w1"].title == "Fix login"
    assert items["w1"].state == "seen"  # default initial state


def test_transition_advances_state_and_records_history(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    _seed(store)
    items = build_projection(store.read_all(), default_machine())
    item = items["w1"]
    assert item.state == "classified"
    assert [h.to for h in item.history] == ["classified"]
    assert item.evidence[0].kind == "task-record"


def test_replay_is_deterministic_across_reload(pm_home: Path) -> None:
    """The core invariant: a crash/restart replays the log to the SAME projection."""
    store = EventStore(resolve_store_dir())
    _seed(store)
    first = build_projection(store.read_all(), default_machine())
    # Simulate a restart: brand-new store object reading the same on-disk log.
    reloaded = EventStore(resolve_store_dir())
    second = build_projection(reloaded.read_all(), default_machine())
    assert first == second


def test_second_intake_for_same_id_links_reference(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.INTAKE, "", {"id": "w1", "title": "first"}))
    store.append(new_event(EventKind.INTAKE, "", {"id": "w1", "title": "second"}))
    items = build_projection(store.read_all(), default_machine())
    assert items["w1"].title == "first"  # first intake wins the fields
    assert len(items["w1"].intake_refs) == 2  # but the duplicate is linked


def test_evidence_and_note_on_unknown_item_are_noops(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.EVIDENCE, "ghost", {"kind": "pr", "uri": "u"}))
    store.append(new_event(EventKind.NOTE, "ghost", {"text": "hi"}))
    assert build_projection(store.read_all(), default_machine()) == {}


def test_illegal_logged_transition_is_skipped_not_crashing(pm_home: Path) -> None:
    """A bad transition in the log must not crash replay — item stays consistent."""
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.INTAKE, "", {"id": "w1", "title": "x"}))
    store.append(new_event(EventKind.TRANSITION, "w1", {"to": "deployed"}))  # illegal from seen
    items = build_projection(store.read_all(), default_machine())
    assert items["w1"].state == "seen"  # unchanged; the illegal jump was rejected
    assert items["w1"].errors  # the rejection is recorded, not silent
