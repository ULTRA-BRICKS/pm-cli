"""Event model + append-only JSONL store: round-trip, ordering, crash-safe append."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pmlib.events import Event, EventKind, new_event
from pmlib.store import EventStore, resolve_store_dir


def test_new_event_generates_id_and_timestamp() -> None:
    ev = new_event(EventKind.INTAKE, "w1", {"title": "hi"})
    assert ev.id and isinstance(ev.id, str)
    assert ev.ts.endswith("Z")  # UTC ISO-8601
    datetime.fromisoformat(ev.ts.replace("Z", "+00:00"))  # parses back — pins the format
    assert ev.kind == EventKind.INTAKE
    assert ev.item_id == "w1"
    assert ev.payload == {"title": "hi"}


def test_event_json_round_trip() -> None:
    ev = new_event(EventKind.TRANSITION, "w1", {"to": "ready"})
    restored = Event.from_json(ev.to_json())
    assert restored == ev


def test_store_append_and_read_preserves_order(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    a = store.append(new_event(EventKind.INTAKE, "w1", {"title": "a"}))
    b = store.append(new_event(EventKind.INTAKE, "w2", {"title": "b"}))
    c = store.append(new_event(EventKind.TRANSITION, "w1", {"to": "ready"}))
    read = store.read_all()
    assert [e.id for e in read] == [a.id, b.id, c.id]
    assert read[2].payload == {"to": "ready"}


def test_store_read_empty_when_no_log(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    assert store.read_all() == []


def test_store_read_skips_blank_and_bad_lines(pm_home: Path) -> None:
    """A truncated/blank line (a crash mid-append) must not crash replay."""
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.INTAKE, "w1", {"title": "a"}))
    log = store.log_path
    with log.open("a", encoding="utf-8") as fh:
        fh.write("\n")  # blank line
        fh.write("{not json\n")  # torn line
    read = store.read_all()
    assert len(read) == 1  # the one good record survives


def test_store_read_skips_a_line_with_a_wrong_shaped_field(pm_home: Path) -> None:
    # Regression: valid JSON but the WRONG SHAPE (e.g. a scalar where "payload" must be an
    # object) raised an uncaught TypeError from `dict(raw.get("payload") or {})` -- a TypeError,
    # not a JSON-decode error, but the same "malformed/foreign line" class every OTHER replay
    # reader in this codebase must tolerate, not crash on.
    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.INTAKE, "w1", {"title": "a"}))
    log = store.log_path
    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"id":"x","ts":"2026-01-01T00:00:00Z","kind":"intake","payload":1}\n')
    read = store.read_all()
    assert len(read) == 1  # the malformed-shape line was skipped, not raised


def test_resolve_store_dir_honors_pm_home(pm_home: Path) -> None:
    assert resolve_store_dir() == pm_home


def test_resolve_store_dir_falls_back_to_xdg(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PM_HOME", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert resolve_store_dir() == tmp_path / "pm"
