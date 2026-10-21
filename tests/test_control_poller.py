"""The proposal poller: one conservative approval per account/chat/turn, never for doubtful state."""

from __future__ import annotations

import pytest

from pmlib.chatgpt_control import api, poller
from pmlib.chatgpt_control.ledger import Ledger
from tests.control_support import CHAT_A, CHAT_B, NOW, combined, make_services, row


def fresh(tmp_path, rows_home=None, rows_glide=None, **kw):
    data = combined(
        {"home": rows_home if rows_home is not None else [row()], "glide.vc": rows_glide or []}
    )
    return make_services(tmp_path, data, **kw)


def requests(services):
    return list(services.snapshot().requests.values())


def test_quiet_complete_chat_gets_exactly_one_proposal(tmp_path):
    services, _, _ = fresh(tmp_path)
    report = poller.poll_once(services)
    (proposal,) = report["proposed"]
    assert proposal["workspace"] == "home" and proposal["turn"] == "turn-1"
    (request,) = requests(services)
    assert (
        request.action == "continue" and request.source == "poller" and request.status == "queued"
    )
    assert "task" in request.reason.lower()
    assert poller.poll_once(services)["proposed"] == []
    assert len(requests(services)) == 1


def test_restart_never_duplicates_the_question(tmp_path):
    services, _, _ = fresh(tmp_path)
    poller.poll_once(services)
    # a brand new process: same state directory, nothing in memory
    reborn, _, _ = fresh(tmp_path)
    reborn.state_dir = services.state_dir
    assert poller.poll_once(reborn)["proposed"] == []
    assert len(Ledger(services.state_dir).read_all()) == 1


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"last_progress_at": NOW - 119}, "not-quiet"),
        ({"last_progress_at": None}, "not-quiet"),
        ({"status": "working", "running": True}, "running"),
        ({"running": True}, "contradictory-completed-and-running"),
        ({"observation": "missing"}, "observation-not-current"),
        ({"observation": "stale"}, "observation-not-current"),
        ({"observation": None}, "observation-not-current"),
        ({"turn": ""}, "missing-turn"),
        ({"session_stale": True}, "session-stale"),
        ({"check_stale": True}, "session-stale"),
        ({"watched": False}, "not-enrolled"),
        ({"reason": "unresolved-send"}, "blocker"),
        ({"reason": "Astra quota is latched"}, "blocker"),
        ({"status": "Thinking failed"}, "failed-turn-uses-recovery"),
        ({"status": "quota-or-rate-limit"}, "quota"),
        ({"draft": True}, "draft-or-attachments"),
        ({"auth_challenge": True}, "auth-challenge"),
    ],
)
def test_doubtful_state_is_never_proposed(tmp_path, overrides, reason):
    services, _, _ = fresh(tmp_path, [row(**overrides)])
    report = poller.poll_once(services)
    assert report["proposed"] == [] and requests(services) == []
    assert report["skipped"].get(reason) == 1


def test_quiet_window_boundary_is_inclusive(tmp_path):
    services, _, _ = fresh(tmp_path, [row(last_progress_at=NOW - 120)])
    assert len(poller.poll_once(services)["proposed"]) == 1


def test_stale_account_snapshot_blocks_everything(tmp_path):
    services, _, _ = make_services(
        tmp_path, combined({"home": [row()], "glide.vc": []}, stale=True)
    )
    report = poller.poll_once(services)
    assert report["proposed"] == [] and report["skipped"].get("account-stale") == 1


def test_one_account_down_does_not_block_the_other(tmp_path):
    data = combined({"home": None, "glide.vc": [row(CHAT_B)]})
    services, _, _ = make_services(tmp_path, data)
    report = poller.poll_once(services)
    assert [p["workspace"] for p in report["proposed"]] == ["glide.vc"]
    assert report["accounts"] == {"home": False, "glide.vc": True}


def test_same_chat_id_in_two_accounts_is_two_proposals(tmp_path):
    data = combined({"home": [row(CHAT_A)], "glide.vc": [row(CHAT_A)]})
    services, _, _ = make_services(tmp_path, data)
    report = poller.poll_once(services)
    assert sorted(p["workspace"] for p in report["proposed"]) == ["glide.vc", "home"]


def settle(services, request_id, outcome):
    ledger = Ledger(services.state_dir)
    now = services.clock()
    ledger.append({"kind": "ask_started", "at": now, "request_id": request_id, "timeout_s": 5})
    ledger.append({"kind": "ask_result", "at": now, "request_id": request_id, "outcome": outcome})


@pytest.mark.parametrize("outcome", ["rejected", "timed_out", "uncertain"])
def test_declined_or_unanswered_turn_is_not_asked_again_but_a_new_turn_is(tmp_path, outcome):
    services, telemetry, clock = fresh(tmp_path)
    poller.poll_once(services)
    settle(services, requests(services)[0].request_id, outcome)
    clock.advance(5000)
    telemetry.data = combined({"home": [row()], "glide.vc": []}, generated_at=clock.now - 5)
    telemetry.data["sessions"][0]["last_progress_at"] = clock.now - 600
    telemetry.data["sessions"][0]["last_checked_at"] = clock.now - 10
    assert poller.poll_once(services)["proposed"] == []
    telemetry.data["sessions"][0].update(turn="turn-2")
    assert len(poller.poll_once(services)["proposed"]) == 1


def test_expired_unasked_request_blocks_a_repeat_for_the_same_turn(tmp_path):
    services, telemetry, clock = fresh(tmp_path)
    poller.poll_once(services)
    clock.advance(2000)
    telemetry.data = combined({"home": [row()], "glide.vc": []}, generated_at=clock.now - 5)
    telemetry.data["sessions"][0]["last_progress_at"] = clock.now - 3000
    assert poller.poll_once(services)["proposed"] == []
    assert requests(services)[0].status == "expired"


def test_delivery_unavailable_retries_only_after_a_cooldown(tmp_path):
    services, telemetry, clock = fresh(tmp_path)
    poller.poll_once(services)
    settle(services, requests(services)[0].request_id, "delivery_unavailable")

    def refresh():
        telemetry.data = combined({"home": [row()], "glide.vc": []}, generated_at=clock.now - 5)
        telemetry.data["sessions"][0]["last_progress_at"] = clock.now - 600
        telemetry.data["sessions"][0]["last_checked_at"] = clock.now - 10

    clock.advance(300)
    refresh()
    assert poller.poll_once(services)["proposed"] == []
    clock.advance(700)
    refresh()
    assert len(poller.poll_once(services)["proposed"]) == 1


def test_an_active_request_for_the_chat_blocks_new_proposals(tmp_path):
    services, _, _ = fresh(tmp_path)
    api.dispatch(
        services, "actions.request", {"workspace": "home", "action": "pause", "chat": CHAT_A}
    )
    report = poller.poll_once(services)
    assert report["proposed"] == [] and report["skipped"].get("chat-busy") == 1


def test_running_without_visible_progress_is_reported_once_and_never_continued(tmp_path):
    stuck = row(
        status="working", running=True, no_visible_progress=True, last_progress_at=NOW - 1000
    )
    services, _, _ = fresh(tmp_path, [stuck])
    first = poller.poll_once(services)
    assert first["proposed"] == [] and len(first["reported"]) == 1
    assert first["reported"][0]["observation"] == "no_visible_progress"
    assert poller.poll_once(services)["reported"] == []
    assert requests(services) == []
    kinds = [e["kind"] for e in Ledger(services.state_dir).read_all()]
    assert kinds == ["observation"]


def test_running_with_progress_is_silent(tmp_path):
    services, _, _ = fresh(
        tmp_path, [row(status="working", running=True, no_visible_progress=False)]
    )
    report = poller.poll_once(services)
    assert report["proposed"] == [] and report["reported"] == []


def test_per_tick_and_active_caps(tmp_path):
    rows = [row(f"https://chatgpt.com/c/chat{i}") for i in range(6)]
    services, _, _ = fresh(tmp_path, rows)
    assert len(poller.poll_once(services)["proposed"]) == poller.MAX_PROPOSALS_PER_TICK
    poller.poll_once(services)
    report = poller.poll_once(services)
    assert report["proposed"] == []
    assert len(requests(services)) == poller.ACTIVE_PROPOSAL_CAP


def test_telemetry_failure_is_reported_not_raised(tmp_path):
    services, _, _ = fresh(tmp_path)

    def boom(_accounts):
        raise RuntimeError("down")

    services.fetch = boom
    report = poller.poll_once(services)
    assert report["proposed"] == [] and report["error"] == "telemetry unavailable"


def test_proposal_report_contains_no_secrets_or_raw_titles(tmp_path):
    services, _, _ = fresh(tmp_path, [row(title="\x1b[31msecret title")])
    report = poller.poll_once(services)
    assert "\x1b" not in str(report)


def test_real_passive_monitor_shape_produces_one_card(tmp_path):
    passive = row(model="Thinking effortPro", effort="", model_verified=False)
    services, _, _ = fresh(tmp_path, [passive])
    assert len(poller.poll_once(services)["proposed"]) == 1
    assert poller.poll_once(services)["proposed"] == []
