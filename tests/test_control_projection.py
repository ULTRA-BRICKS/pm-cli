"""Pure fold of ledger events into request receipts; time only enters through ``now``."""

from __future__ import annotations

import pytest

from pmlib.chatgpt_control.projection import ACTIVE, project

ID = "11111111-1111-4111-8111-111111111111"
T0 = 1000.0


def ev(kind, at, **kw):
    return {"v": 1, "kind": kind, "at": at, **kw}


def created(at=T0, **kw):
    fields = {
        "request_id": ID,
        "dedup_key": "k" * 64,
        "source": "api",
        "account": "home",
        "chat": "https://chatgpt.com/c/alpha",
        "chat_title": "A",
        "expected_turn": "turn-1",
        "action": "continue",
        "plan_digest": "d" * 64,
        "reason": "",
        "expires_at": int(at) + 900,
    }
    fields.update(kw)
    return ev("request_created", at, **fields)


def status(events, now=T0 + 1):
    return project(events, now).requests[ID].status


def test_created_is_queued():
    assert status([created()]) == "queued"


def test_queue_expires_by_clock_only():
    assert status([created()], now=T0 + 899) == "queued"
    assert status([created()], now=T0 + 900) == "expired"


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("approved", "approved"),
        ("rejected", "rejected"),
        ("timed_out", "timed_out"),
        ("delivery_unavailable", "delivery_unavailable"),
        ("uncertain", "asked_post_uncertain"),
    ],
)
def test_owner_outcomes(outcome, expected):
    events = [
        created(),
        ev("ask_started", T0 + 1, request_id=ID, timeout_s=300, question_digest="q"),
        ev(
            "ask_result",
            T0 + 2,
            request_id=ID,
            outcome=outcome,
            exit_code=0 if outcome == "approved" else 1,
        ),
    ]
    assert status(events) == expected


def test_ask_without_result_is_asked_then_post_uncertain_after_deadline():
    events = [
        created(),
        ev("ask_started", T0 + 1, request_id=ID, timeout_s=300, question_digest="q"),
    ]
    assert status(events, now=T0 + 100) == "asked"
    assert status(events, now=T0 + 1 + 300 + 31) == "asked_post_uncertain"


def test_abandoned_ask_is_post_uncertain_never_requeued():
    events = [
        created(),
        ev("ask_started", T0 + 1, request_id=ID, timeout_s=300, question_digest="q"),
        ev("ask_abandoned", T0 + 5, request_id=ID, detail="worker restarted"),
    ]
    assert status(events) == "asked_post_uncertain"
    assert "asked_post_uncertain" not in ACTIVE


def approved_events():
    return [
        created(),
        ev("ask_started", T0 + 1, request_id=ID, timeout_s=300, question_digest="q"),
        ev("ask_result", T0 + 2, request_id=ID, outcome="approved", exit_code=0),
    ]


def test_dispatch_lifecycle_to_sent():
    events = approved_events() + [
        ev("dispatch_started", T0 + 3, request_id=ID, approved_at=T0 + 2, expires_at=T0 + 900),
    ]
    assert status(events) == "dispatching"
    events.append(ev("dispatch_result", T0 + 4, request_id=ID, outcome="queued", detail=""))
    assert status(events) == "dispatch_queued"
    events.append(ev("continuation_status", T0 + 8, request_id=ID, state="sent", detail=""))
    assert status(events) == "sent"


def test_dispatch_without_result_becomes_uncertain_never_retried():
    events = approved_events() + [ev("dispatch_started", T0 + 3, request_id=ID, approved_at=T0 + 2)]
    assert status(events, now=T0 + 3 + 60) == "dispatching"
    assert status(events, now=T0 + 3 + 121) == "uncertain"


def test_uncertain_dispatch_can_be_reconciled_by_guardian_status_only():
    events = approved_events() + [
        ev("dispatch_started", T0 + 3, request_id=ID, approved_at=T0 + 2),
        ev("dispatch_result", T0 + 4, request_id=ID, outcome="uncertain", detail="timeout"),
        ev("continuation_status", T0 + 9, request_id=ID, state="sent", detail=""),
    ]
    assert status(events) == "sent"


def test_invalidation_records_reason():
    events = approved_events() + [
        ev(
            "request_invalidated",
            T0 + 3,
            request_id=ID,
            reason="turn-changed",
            stage="pre-dispatch",
        )
    ]
    state = project(events, T0 + 4).requests[ID]
    assert (state.status, state.status_reason) == ("invalidated", "turn-changed")


def test_approved_but_never_dispatched_expires():
    assert status(approved_events(), now=T0 + 901) == "expired"


def test_illegal_transitions_are_recorded_not_applied():
    events = [
        created(),
        ev("dispatch_result", T0 + 1, request_id=ID, outcome="queued"),  # never approved
        ev("ask_result", T0 + 2, request_id=ID, outcome="approved", exit_code=0),  # never asked
    ]
    proj = project(events, T0 + 3)
    assert proj.requests[ID].status == "queued"
    assert len(proj.errors) == 2


def test_terminal_states_are_not_reopened():
    events = [
        created(),
        ev("ask_started", T0 + 1, request_id=ID, timeout_s=300, question_digest="q"),
        ev("ask_result", T0 + 2, request_id=ID, outcome="rejected", exit_code=1),
        ev("ask_result", T0 + 3, request_id=ID, outcome="approved", exit_code=0),
    ]
    assert status(events) == "rejected"


def test_projection_is_deterministic_and_ignores_unknown_and_orphan_events():
    events = [
        ev("ask_started", T0, request_id="orphan", timeout_s=1, question_digest="q"),
        ev("mystery", T0),
        created(),
    ]
    first, second = project(events, T0 + 1), project(list(events), T0 + 1)
    assert first.requests.keys() == second.requests.keys() == {ID}
    assert first.requests[ID].receipt() == second.requests[ID].receipt()


def test_receipt_has_frozen_keys_and_no_secrets():
    receipt = project([created()], T0 + 1).requests[ID].receipt()
    assert {
        "request_id",
        "status",
        "action",
        "workspace",
        "chat",
        "expected_turn",
        "expires_at",
    } <= set(receipt)
    assert receipt["workspace"] == "home"
    assert "token" not in str(receipt).lower()


def test_observations_are_collected():
    proj = project([ev("observation", T0, key="k1", obs="no_visible_progress")], T0)
    assert [o["key"] for o in proj.observations] == ["k1"]
