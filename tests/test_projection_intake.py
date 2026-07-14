"""Folding intake events into the ledger — captured/failed/processed/deadletter, deterministic."""

from __future__ import annotations

from pathlib import Path

from pmlib.events import EventKind, new_event
from pmlib.projection import build_intake_ledger
from pmlib.store import EventStore, resolve_store_dir


def _cap(iid: str = "tg:1", text: str = "hi") -> object:
    return new_event(EventKind.INTAKE_CAPTURED, "", {
        "id": iid, "source": "telegram", "external_id": iid.split(":")[-1],
        "sender": "Alex", "text": text, "ts_ms": 123, "pane": "%1"})


def test_captured_event_creates_unprocessed_intake() -> None:
    ledger = build_intake_ledger([_cap()])
    assert ledger["tg:1"].status == "unprocessed"
    assert ledger["tg:1"].text == "hi"
    assert ledger["tg:1"].attempts == 0


def test_failed_event_increments_attempts_and_records_reason() -> None:
    events = [_cap(), new_event(EventKind.INTAKE_FAILED, "tg:1", {"reason": "boom"})]
    ledger = build_intake_ledger(events)
    assert ledger["tg:1"].attempts == 1
    assert ledger["tg:1"].last_error == "boom"
    assert ledger["tg:1"].status == "unprocessed"  # still retryable


def test_processed_event_marks_processed_and_links_work_item() -> None:
    events = [_cap(), new_event(EventKind.INTAKE_PROCESSED, "tg:1", {"work_item_id": "intake:tg:1"})]
    ledger = build_intake_ledger(events)
    assert ledger["tg:1"].status == "processed"
    assert ledger["tg:1"].work_item_id == "intake:tg:1"


def test_deadletter_event_marks_deadletter() -> None:
    events = [_cap(), new_event(EventKind.INTAKE_DEADLETTER, "tg:1", {"reason": "gave up"})]
    ledger = build_intake_ledger(events)
    assert ledger["tg:1"].status == "deadletter"
    assert ledger["tg:1"].last_error == "gave up"


def test_ledger_replay_is_deterministic(pm_home: Path) -> None:
    store = EventStore(resolve_store_dir())
    store.append(_cap())
    store.append(new_event(EventKind.INTAKE_FAILED, "tg:1", {"reason": "x"}))
    first = build_intake_ledger(store.read_all())
    second = build_intake_ledger(EventStore(resolve_store_dir()).read_all())
    assert first == second


def test_events_for_unknown_intake_are_ignored() -> None:
    ledger = build_intake_ledger([new_event(EventKind.INTAKE_FAILED, "ghost", {"reason": "x"})])
    assert ledger == {}
