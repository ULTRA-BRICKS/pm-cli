"""The telegram adapter: inbound records -> capture events, deduped by message id."""

from __future__ import annotations

from pmlib.adapters.telegram import default_processor, plan_capture
from pmlib.events import EventKind
from pmlib.intake import IntakeEvent
from pmlib.projection import build_intake_ledger

_RECORDS = [
    {"ts": 100, "id": 8328, "direction": "user", "from": "Alex", "text": "deploy it", "pane": "%4"},
    {"ts": 200, "id": 8331, "direction": "user", "from": "Alex", "text": "status?", "pane": None},
]


def test_plan_capture_records_each_message_as_unprocessed() -> None:
    events = plan_capture(_RECORDS, {})
    assert all(e.kind == EventKind.INTAKE_CAPTURED for e in events)
    ledger = build_intake_ledger(events)
    assert set(ledger) == {"tg:8328", "tg:8331"}
    assert ledger["tg:8328"].status == "unprocessed"
    assert ledger["tg:8328"].sender == "Alex"
    assert ledger["tg:8328"].text == "deploy it"


def test_plan_capture_dedups_already_captured() -> None:
    first = build_intake_ledger(plan_capture(_RECORDS, {}))
    assert plan_capture(_RECORDS, first) == []  # re-capture is a no-op


def test_plan_capture_skips_records_without_id() -> None:
    assert plan_capture([{"from": "Alex", "text": "no id"}], {}) == []


def test_default_processor_builds_a_work_item_payload() -> None:
    intake = IntakeEvent(id="tg:1", source="telegram", external_id="1", sender="Alex",
                         text="fix the login bug please")
    payload = default_processor(intake)
    assert payload["id"] == "intake:tg:1"
    assert payload["title"].startswith("fix the login")
    assert payload["state"] == "seen"
    assert payload["evidence"][0]["kind"] == "telegram-message"


def test_default_processor_handles_empty_text() -> None:
    intake = IntakeEvent(id="tg:2", source="telegram", external_id="2", sender="Alex", text="")
    payload = default_processor(intake)
    assert payload["title"]  # a non-empty placeholder title, never blank
