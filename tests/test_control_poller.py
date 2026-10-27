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


# --- cached audit verdicts: they can only make the poller MORE cautious -------------------------

CONV = "0123abcd-0123-4abc-8def-0123456789ab"
CONV_CHAT = f"https://chatgpt.com/c/{CONV}"
PROJECT = "g-p-" + "a" * 32


@pytest.fixture(autouse=True)
def hermetic_audit(tmp_path, monkeypatch):
    """No test may read the real audit store dir or look for a real classifier socket."""
    monkeypatch.setenv("PM_HOME", str(tmp_path / "pm-home"))
    monkeypatch.setattr(poller, "default_expected_model", lambda: "")


PROFILE = (
    "convaiinnovations/laya-multilingual@2048|convaiinnovations/laya@2048|"
    "convaiinnovations/laya@512"
)
ENGLISH_META = {"checkpoint": "convaiinnovations/laya", "max_len": 512, "truncated": False}


def entry(task_state="done", **kw):
    """A cached verdict for the row's current turn (``turn-1``), made under the live profile."""
    from pmlib.chatgpt_control import audit_classifier, audit_decision, audit_store

    modelled = task_state in ("done", "incomplete", "unknown")
    base = {
        "account": "home",
        "conversation_id": CONV,
        "fingerprint": "fp",
        "evidence_digest": "digest",
        "request_id": "u1",
        "final_id": "a1",
        "turn_ids": ("turn-1",),
        "last_message_id": "m1",
        "updated_at": NOW - 700,
        "verified_at": NOW - 60,
        "prompt_version": audit_classifier.PROMPT_VERSION,
        "model": "convaiinnovations/laya@512" if modelled else "",
        "thresholds_digest": audit_decision.thresholds_digest(audit_decision.DEFAULT_THRESHOLDS),
        "verdict": {
            "task_state": task_state,
            "classifier_status": "ok" if modelled else "not_needed",
            "classifier_meta": dict(ENGLISH_META) if modelled else {},
        },
    }
    base.update(kw)
    return audit_store.CachedVerdict(**base)


def audited(tmp_path, cached, chat=CONV_CHAT, expected_model=PROFILE, **row_kw):
    from pmlib.chatgpt_control import audit_store

    services, _, _ = fresh(tmp_path, [row(chat, **row_kw)])
    services.audit_dir = tmp_path / "audit"
    store = audit_store.AuditStore(services.audit_dir)
    for item in cached if isinstance(cached, list) else [cached]:
        store.put(item)
    return services, lambda: expected_model


def poll(services, model):
    return poller.poll_once(services, expected_model=model)


@pytest.mark.parametrize(
    "state,reason",
    [("done", "task-finished"), ("working", "task-working"), ("blocked", "task-blocked")],
)
def test_trusted_verdict_suppresses_the_owner_question(tmp_path, state, reason):
    services, model = audited(tmp_path, entry(state))
    report = poll(services, model)
    assert report["proposed"] == [] and requests(services) == []
    assert report["skipped"] == {reason: 1}


@pytest.mark.parametrize("state", ["incomplete", "unknown"])
def test_incomplete_or_unknown_verdict_still_proposes(tmp_path, state):
    services, model = audited(tmp_path, entry(state))
    assert len(poll(services, model)["proposed"]) == 1


def test_no_verdict_at_all_still_proposes(tmp_path):
    services, model = audited(tmp_path, [])
    assert len(poll(services, model)["proposed"]) == 1


def test_corrupt_store_file_still_proposes(tmp_path):
    services, model = audited(tmp_path, [])
    (tmp_path / "audit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "audit" / "verdicts.json").write_text("{not json")
    assert len(poll(services, model)["proposed"]) == 1


def test_store_path_that_is_a_directory_still_proposes(tmp_path):
    services, model = audited(tmp_path, [])
    (tmp_path / "audit" / "verdicts.json").mkdir(parents=True)
    assert len(poll(services, model)["proposed"]) == 1


def test_stale_verdict_proposes(tmp_path):
    services, model = audited(tmp_path, entry(verified_at=NOW - poller.AUDIT_VERDICT_MAX_AGE - 1))
    assert len(poll(services, model)["proposed"]) == 1


def test_verdict_at_exactly_max_age_is_still_trusted(tmp_path):
    services, model = audited(
        tmp_path,
        entry(verified_at=NOW - poller.AUDIT_VERDICT_MAX_AGE),
        last_progress_at=NOW - poller.AUDIT_VERDICT_MAX_AGE - 100,
    )
    assert poll(services, model)["skipped"] == {"task-finished": 1}


@pytest.mark.parametrize("field", ["prompt_version", "thresholds_digest"])
def test_version_mismatch_proposes(tmp_path, field):
    services, model = audited(tmp_path, entry(**{field: "other"}))
    assert len(poll(services, model)["proposed"]) == 1


def test_model_mismatch_proposes(tmp_path):
    services, model = audited(
        tmp_path, entry(), expected_model="convaiinnovations/laya-multilingual@2048"
    )
    assert len(poll(services, model)["proposed"]) == 1


def test_model_dependent_verdict_with_unknown_model_proposes(tmp_path):
    services, model = audited(tmp_path, entry(), expected_model="")
    assert len(poll(services, model)["proposed"]) == 1


def test_model_dependent_verdict_with_matching_model_is_trusted(tmp_path):
    services, model = audited(tmp_path, entry())
    assert poll(services, model)["skipped"] == {"task-finished": 1}


def test_a_blocked_verdict_needs_no_model_so_a_classifier_outage_keeps_it(tmp_path):
    services, model = audited(tmp_path, entry("blocked"), expected_model="")
    assert poll(services, model)["skipped"] == {"task-blocked": 1}


def test_a_done_verdict_with_the_classifier_down_never_suppresses(tmp_path):
    services, model = audited(tmp_path, entry("done"), expected_model="")
    assert len(poll(services, model)["proposed"]) == 1


@pytest.mark.parametrize("state", ["done", "working", "blocked"])
@pytest.mark.parametrize("progress", [NOW - 600, NOW - 130])
def test_a_new_request_with_old_or_lagging_progress_is_not_suppressed(tmp_path, state, progress):
    # The cached verdict describes turn-0; the row now shows turn-1, a NEW user request, while the
    # progress metadata still says nothing happened after the verification.
    services, model = audited(
        tmp_path, entry(state, turn_ids=("turn-0",)), last_progress_at=progress, turn="turn-1"
    )
    report = poll(services, model)
    assert len(report["proposed"]) == 1 and report["skipped"] == {}


def test_a_verdict_without_any_recorded_turn_identity_never_suppresses(tmp_path):
    services, model = audited(tmp_path, entry(turn_ids=()))
    assert len(poll(services, model)["proposed"]) == 1


def test_any_recorded_native_identifier_of_the_current_turn_proves_the_identity(tmp_path):
    services, model = audited(tmp_path, entry(turn_ids=("msg-user", "turn-1", "msg-final")))
    assert poll(services, model)["skipped"] == {"task-finished": 1}


@pytest.mark.parametrize(
    "meta",
    [
        {**ENGLISH_META, "truncated": True},  # a cut input can never have decided done
        {**ENGLISH_META, "truncated": None},
        {**ENGLISH_META, "truncated": "false"},
        {**ENGLISH_META, "max_len": 2048},  # not the window the entry's model names
        {**ENGLISH_META, "checkpoint": "convaiinnovations/laya-multilingual"},
        {"checkpoint": "convaiinnovations/laya", "max_len": 512},  # accounting missing
        {},
    ],
)
def test_a_done_verdict_without_proven_non_truncated_route_meta_never_suppresses(tmp_path, meta):
    cached = entry()
    verdict = dict(cached.verdict, classifier_meta=meta)
    services, model = audited(tmp_path, entry(verdict=verdict))
    assert len(poll(services, model)["proposed"]) == 1


def test_a_done_verdict_whose_classifier_status_is_not_ok_never_suppresses(tmp_path):
    verdict = dict(entry().verdict, classifier_status="unavailable")
    services, model = audited(tmp_path, entry(verdict=verdict))
    assert len(poll(services, model)["proposed"]) == 1


def test_a_done_verdict_with_no_model_never_suppresses(tmp_path):
    services, model = audited(tmp_path, entry(model=""))
    assert len(poll(services, model)["proposed"]) == 1


def test_progress_after_verification_proposes(tmp_path):
    services, model = audited(tmp_path, entry(verified_at=NOW - 700), last_progress_at=NOW - 600)
    assert len(poll(services, model)["proposed"]) == 1


def test_progress_exactly_at_verification_keeps_the_verdict(tmp_path):
    services, model = audited(tmp_path, entry(verified_at=NOW - 600), last_progress_at=NOW - 600)
    assert poll(services, model)["skipped"] == {"task-finished": 1}


def test_running_row_is_decided_by_existing_gates_not_the_verdict(tmp_path):
    services, model = audited(tmp_path, entry("done"), running=True)
    report = poll(services, model)
    assert report["skipped"] == {"contradictory-completed-and-running": 1}
    services, model = audited(tmp_path / "w", entry("done"), status="working", running=True)
    assert poll(services, model)["skipped"] == {"running": 1}


def test_non_current_observation_gets_no_override(tmp_path):
    services, model = audited(tmp_path, entry("done"), observation="stale")
    assert poll(services, model)["skipped"] == {"observation-not-current": 1}


def test_verdict_for_another_account_is_not_used(tmp_path):
    services, model = audited(tmp_path, entry(account="glide.vc"))
    assert len(poll(services, model)["proposed"]) == 1


def test_verdict_for_another_conversation_is_not_used(tmp_path):
    other = "11111111-1111-4111-8111-111111111111"
    services, model = audited(tmp_path, entry(conversation_id=other))
    assert len(poll(services, model)["proposed"]) == 1


def test_project_prefixed_chat_url_resolves_to_the_same_conversation(tmp_path):
    chat = f"https://chatgpt.com/g/{PROJECT}/c/{CONV}"
    services, model = audited(tmp_path, entry(), chat=chat)
    assert poll(services, model)["skipped"] == {"task-finished": 1}


def test_unparseable_chat_url_behaves_as_before(tmp_path):
    services, model = audited(tmp_path, entry(), chat="https://chatgpt.com/not-a-chat")
    assert len(poll(services, model)["proposed"]) == 1


def test_classifier_outage_verdict_never_skips(tmp_path):
    outage = entry("unknown", verdict={"task_state": "unknown", "classifier_status": "unavailable"})
    services, model = audited(tmp_path, outage)
    report = poll(services, model)
    assert len(report["proposed"]) == 1 and report["skipped"] == {}


def test_failing_model_lookup_never_skips_and_still_proposes(tmp_path):
    services, _ = audited(tmp_path, entry("done"))

    def boom():
        raise OSError("no socket dir")

    assert len(poller.poll_once(services, expected_model=boom)["proposed"]) == 1


def test_the_provider_profile_is_probed_once_per_tick_not_once_per_row(tmp_path):
    from pmlib.chatgpt_control import audit_store

    other = "11111111-1111-4111-8111-111111111111"
    other_chat = f"https://chatgpt.com/c/{other}"
    services, _, _ = fresh(tmp_path, [row(CONV_CHAT), row(other_chat)])
    services.audit_dir = tmp_path / "audit"
    store = audit_store.AuditStore(services.audit_dir)
    store.put(entry())
    store.put(entry(conversation_id=other))
    probes = []

    def counting():
        probes.append(1)
        return PROFILE

    report = poller.poll_once(services, expected_model=counting)
    assert report["skipped"] == {"task-finished": 2} and len(probes) == 1


def test_poller_never_writes_the_audit_store(tmp_path):
    services, model = audited(tmp_path, entry("incomplete"))
    before = {p: p.read_bytes() for p in (tmp_path / "audit").rglob("*") if p.is_file()}
    poll(services, model)
    poll(services, model)
    after = {p: p.read_bytes() for p in (tmp_path / "audit").rglob("*") if p.is_file()}
    assert before == after and before


def test_poller_does_not_create_a_missing_audit_dir(tmp_path):
    services, model = audited(tmp_path, [])
    assert not (tmp_path / "audit").exists()
    poll(services, model)
    assert not (tmp_path / "audit").exists()
