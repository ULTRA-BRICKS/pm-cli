"""The intake dead-letter ledger: bounded-retry processing, deadletter on exhaustion."""

from __future__ import annotations

from pmlib.events import EventKind
from pmlib.intake import MAX_ATTEMPTS, IntakeEvent, plan_processing


def _intake(iid: str = "tg:1", attempts: int = 0, status: str = "unprocessed",
            text: str = "hello") -> IntakeEvent:
    return IntakeEvent(id=iid, source="telegram", external_id=iid.split(":")[-1],
                       sender="Alex", text=text, status=status, attempts=attempts)


def _ok_processor(intake: IntakeEvent) -> dict:
    return {"id": f"intake:{intake.id}", "title": intake.text[:80], "state": "seen"}


def _fail_processor(intake: IntakeEvent) -> dict:
    raise ValueError("boom")


def test_successful_processing_emits_workitem_and_processed() -> None:
    events = plan_processing({"tg:1": _intake()}, _ok_processor)
    kinds = [e.kind for e in events]
    assert EventKind.INTAKE in kinds  # the WorkItem is born
    assert EventKind.INTAKE_PROCESSED in kinds  # ...and the intake is marked processed
    processed = next(e for e in events if e.kind == EventKind.INTAKE_PROCESSED)
    assert processed.payload["work_item_id"] == "intake:tg:1"


def test_failure_under_bound_emits_failed_not_deadletter() -> None:
    events = plan_processing({"tg:1": _intake(attempts=0)}, _fail_processor)
    assert [e.kind for e in events] == [EventKind.INTAKE_FAILED]
    assert "boom" in events[0].payload["reason"]


def test_failure_at_bound_deadletters() -> None:
    events = plan_processing({"tg:1": _intake(attempts=MAX_ATTEMPTS - 1)}, _fail_processor)
    assert [e.kind for e in events] == [EventKind.INTAKE_DEADLETTER]
    assert "boom" in events[0].payload["reason"]


def test_malformed_payload_is_a_failure_not_a_crash() -> None:
    """A processor returning an id-less / non-dict payload must retry, never crash the run."""
    for bad in (lambda i: {"title": "no id"}, lambda i: "not a dict", lambda i: None):
        events = plan_processing({"tg:1": _intake(attempts=0)}, bad)
        assert [e.kind for e in events] == [EventKind.INTAKE_FAILED]
        assert "id" in events[0].payload["reason"].lower()


def test_processed_and_deadletter_intakes_are_skipped() -> None:
    intakes = {
        "a": _intake(iid="a", status="processed"),
        "b": _intake(iid="b", status="deadletter"),
    }
    assert plan_processing(intakes, _fail_processor) == []


def test_bounded_retries_reach_deadletter_after_max_attempts() -> None:
    """Simulate repeated runs: each failure increments attempts until it deadletters."""
    attempts = 0
    status = "unprocessed"
    for _ in range(MAX_ATTEMPTS + 2):  # more runs than the bound — must terminate at deadletter
        if status != "unprocessed":
            break
        events = plan_processing({"tg:1": _intake(attempts=attempts)}, _fail_processor)
        ev = events[0]
        if ev.kind == EventKind.INTAKE_DEADLETTER:
            status = "deadletter"
        else:
            attempts += 1
    assert status == "deadletter"
    assert attempts == MAX_ATTEMPTS - 1  # deadletter fired on the attempt that hit the bound
