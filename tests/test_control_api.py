"""Frozen API behaviour through the single dispatch: reads, request-only writes, dedup, isolation."""

from __future__ import annotations

import json
import threading

from pmlib.chatgpt_control import api
from pmlib.chatgpt_control.ledger import Ledger
from tests.control_support import CHAT_A, CHAT_B, NOW, combined, make_services, row


def call(services, method, **params):
    return api.dispatch(services, method, params)


def codes(envelope):
    assert envelope["ok"] is False
    return envelope["error"]["code"]


def test_envelope_shapes(tmp_path):
    services, _, _ = make_services(tmp_path)
    ok = call(services, "accounts.list")
    assert ok == {
        "ok": True,
        "result": {"accounts": [{"workspace": "home"}, {"workspace": "glide.vc"}]},
    }
    bad = call(services, "nope")
    assert set(bad) == {"ok", "error"} and set(bad["error"]) == {"code", "message"}
    assert bad["error"]["code"] == "unknown_method"
    json.dumps(ok)
    json.dumps(bad)


def test_unexpected_exception_is_generic_and_leaks_nothing(tmp_path):
    services, _, _ = make_services(tmp_path)

    def boom(_accounts):
        raise RuntimeError("Bearer SECRET-TOKEN-123 at /home/ultra/.config")

    services.fetch = boom
    out = call(services, "sessions.list")
    assert codes(out) == "internal_error"
    assert "SECRET" not in json.dumps(out) and "/home" not in json.dumps(out)


def test_sessions_list_all_and_single(tmp_path):
    services, telemetry, _ = make_services(tmp_path)
    everything = call(services, "sessions.list")["result"]
    assert everything["workspace"] == "all"
    assert {s["account"] for s in everything["sessions"]} == {"home", "glide.vc"}
    one = call(services, "sessions.list", workspace="glide.vc")["result"]
    assert one["workspace"] == "glide.vc"
    assert {s["account"] for s in one["sessions"]} == {"glide.vc"}
    assert telemetry.calls[-1] == ("glide.vc",)


def test_one_account_down_does_not_hide_the_other(tmp_path):
    data = combined({"home": None, "glide.vc": [row(CHAT_B)]})
    services, _, _ = make_services(tmp_path, data)
    result = call(services, "sessions.list")["result"]
    states = {a["workspace"]: a["ok"] for a in result["accounts"]}
    assert states == {"home": False, "glide.vc": True}
    assert len(result["sessions"]) == 1


def test_sessions_get_is_account_scoped(tmp_path):
    data = combined(
        {"home": [row(CHAT_A, title="home one")], "glide.vc": [row(CHAT_A, title="glide one")]}
    )
    services, _, _ = make_services(tmp_path, data)
    out = call(services, "sessions.get", workspace="glide.vc", chat=CHAT_A)["result"]
    assert out["session"]["title"] == "glide one" and out["session"]["account"] == "glide.vc"
    assert out["account"]["workspace"] == "glide.vc" and "sessions" not in out["account"]
    other = call(services, "sessions.get", workspace="home", chat=CHAT_B)
    assert codes(other) == "not_found"


def test_sessions_get_down_account_is_unavailable_not_empty(tmp_path):
    data = combined({"home": None, "glide.vc": [row()]})
    services, _, _ = make_services(tmp_path, data)
    assert (
        codes(call(services, "sessions.get", workspace="home", chat=CHAT_A))
        == "account_unavailable"
    )


def request(services, **params):
    return call(services, "actions.request", **params)


def test_continue_request_binds_the_observed_turn_and_queues_only(tmp_path):
    services, _, _ = make_services(tmp_path)
    out = request(services, workspace="glide.vc", chat=CHAT_B, action="continue")
    assert out["ok"], out
    result = out["result"]
    assert result["status"] == "queued" and result["expected_turn"] == "turn-1"
    assert result["workspace"] == "glide.vc" and result["chat"] == CHAT_B
    assert result["expires_at"] == int(NOW) + 900 and result["duplicate"] is False
    kinds = [e["kind"] for e in Ledger(services.state_dir).read_all()]
    assert kinds == ["request_created"]  # no ask, no dispatch from the API


def test_exact_turn_mismatch_is_refused(tmp_path):
    services, _, _ = make_services(tmp_path)
    out = request(
        services, workspace="home", chat=CHAT_A, action="continue", expected_turn="turn-0"
    )
    assert codes(out) == "refused" and "turn-changed" in out["error"]["message"]


def test_running_and_blocked_chats_are_refused_at_request_time(tmp_path):
    for overrides in ({"status": "working", "running": True}, {"reason": "unresolved-send"}):
        data = combined({"home": [row(**overrides)], "glide.vc": []})
        services, _, _ = make_services(tmp_path / str(len(overrides)), data)
        out = request(services, workspace="home", chat=CHAT_A, action="continue")
        assert codes(out) == "refused"
        assert Ledger(services.state_dir).read_all() == []


def test_duplicate_request_returns_original_receipt_once(tmp_path):
    services, _, clock = make_services(tmp_path)
    first = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    clock.advance(30)
    again = request(services, workspace="home", chat=CHAT_A, action="continue", reason="other")[
        "result"
    ]
    assert again["request_id"] == first["request_id"] and again["duplicate"] is True
    assert again["expires_at"] == first["expires_at"]
    assert len(Ledger(services.state_dir).read_all()) == 1


def test_concurrent_duplicates_create_one_request(tmp_path):
    services, _, _ = make_services(tmp_path)
    results = []

    def go():
        results.append(request(services, workspace="home", chat=CHAT_A, action="continue"))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({r["result"]["request_id"] for r in results}) == 1
    assert len(Ledger(services.state_dir).read_all()) == 1


def test_same_chat_in_two_accounts_are_distinct_requests(tmp_path):
    data = combined({"home": [row(CHAT_A)], "glide.vc": [row(CHAT_A)]})
    services, _, _ = make_services(tmp_path, data)
    a = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    b = request(services, workspace="glide.vc", chat=CHAT_A, action="continue")["result"]
    assert a["request_id"] != b["request_id"]
    assert (a["workspace"], b["workspace"]) == ("home", "glide.vc")
    assert a["plan_digest"] != b["plan_digest"]


def test_chat_from_other_account_is_not_found(tmp_path):
    services, _, _ = make_services(tmp_path)  # CHAT_A lives in home only
    for action in ("continue", "watch", "pause"):
        assert (
            codes(request(services, workspace="glide.vc", chat=CHAT_A, action=action))
            == "not_found"
        )


def test_new_turn_is_a_new_request_old_turn_stays_deduped(tmp_path):
    services, telemetry, _ = make_services(tmp_path)
    first = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    telemetry.data["sessions"] = [dict(row(CHAT_A, turn="turn-2"), account="home")]
    second = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    assert second["request_id"] != first["request_id"] and second["expected_turn"] == "turn-2"


def test_controls_queue_with_existing_planner_rules(tmp_path):
    services, _, _ = make_services(tmp_path)
    for action in ("watch", "unwatch", "pause", "resume"):
        out = request(services, workspace="home", chat=CHAT_A, action=action)
        assert (
            out["ok"]
            and out["result"]["status"] == "queued"
            and out["result"]["expected_turn"] is None
        )
    assert len(Ledger(services.state_dir).read_all()) == 4


def test_service_stop_works_with_the_account_down_but_start_needs_fresh_telemetry(tmp_path):
    data = combined({"home": None, "glide.vc": [row()]})
    services, telemetry, _ = make_services(tmp_path, data)
    assert request(services, workspace="home", action="service-stop")["ok"]
    assert telemetry.calls == []  # no telemetry needed to stop
    assert (
        codes(request(services, workspace="home", action="service-start")) == "account_unavailable"
    )


def test_pause_with_account_down_is_unavailable(tmp_path):
    data = combined({"home": None, "glide.vc": [row()]})
    services, _, _ = make_services(tmp_path, data)
    assert (
        codes(request(services, workspace="home", chat=CHAT_A, action="pause"))
        == "account_unavailable"
    )


def test_non_turn_actions_dedupe_only_while_active(tmp_path):
    services, _, clock = make_services(tmp_path)
    first = request(services, workspace="home", chat=CHAT_A, action="pause")["result"]
    assert (
        request(services, workspace="home", chat=CHAT_A, action="pause")["result"]["request_id"]
        == first["request_id"]
    )
    clock.advance(901)  # the queued request expired: a new explicit request is allowed
    assert (
        request(services, workspace="home", chat=CHAT_A, action="pause")["result"]["request_id"]
        != first["request_id"]
    )


def test_continue_duplicates_survive_expiry(tmp_path):
    services, _, clock = make_services(tmp_path)
    first = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    clock.advance(901)
    again = request(
        services, workspace="home", chat=CHAT_A, action="continue", expected_turn="turn-1"
    )["result"]
    assert again["request_id"] == first["request_id"] and again["duplicate"] is True
    assert again["status"] == "expired"


def test_continue_after_rejection_is_not_asked_again(tmp_path):
    services, _, clock = make_services(tmp_path)
    first = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    ledger = Ledger(services.state_dir)
    rid = first["request_id"]
    ledger.append({"kind": "ask_started", "at": clock.now, "request_id": rid, "timeout_s": 5})
    ledger.append({"kind": "ask_result", "at": clock.now, "request_id": rid, "outcome": "rejected"})
    again = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    assert again["request_id"] == rid and again["status"] == "rejected"


def test_delivery_unavailable_allows_one_explicit_new_request(tmp_path):
    services, _, clock = make_services(tmp_path)
    one = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    ledger = Ledger(services.state_dir)
    rid = one["request_id"]
    ledger.append({"kind": "ask_started", "at": clock.now, "request_id": rid, "timeout_s": 5})
    ledger.append(
        {
            "kind": "ask_result",
            "at": clock.now,
            "request_id": rid,
            "outcome": "delivery_unavailable",
        }
    )
    two = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    assert two["request_id"] != rid and two["status"] == "queued"
    again = request(services, workspace="home", chat=CHAT_A, action="continue")["result"]
    assert again["request_id"] == two["request_id"]


def test_active_request_cap(tmp_path):
    from pmlib.chatgpt_control.service import MAX_ACTIVE

    rows = [row(f"https://chatgpt.com/c/chat{i}") for i in range(MAX_ACTIVE + 1)]
    services, _, _ = make_services(tmp_path, combined({"home": rows, "glide.vc": []}))
    for r in rows[:MAX_ACTIVE]:
        assert request(services, workspace="home", chat=r["chat"], action="pause")["ok"]
    assert (
        codes(request(services, workspace="home", chat=rows[-1]["chat"], action="pause")) == "busy"
    )


def test_requests_list_and_get(tmp_path):
    services, _, clock = make_services(tmp_path)
    a = request(services, workspace="home", chat=CHAT_A, action="pause")["result"]
    clock.advance(1)
    b = request(services, workspace="glide.vc", chat=CHAT_B, action="pause")["result"]
    listing = call(services, "requests.list")["result"]["requests"]
    assert [r["request_id"] for r in listing] == [b["request_id"], a["request_id"]]
    got = call(services, "requests.get", request_id=a["request_id"])["result"]
    assert got["status"] == "queued" and got["timeline"][0]["event"] == "request_created"
    assert (
        codes(call(services, "requests.get", request_id="0f8fad5b-d9cb-469f-a165-70867728950e"))
        == "not_found"
    )
    assert "token" not in json.dumps(got).lower()


def test_forbidden_and_unknown_keys_never_reach_the_ledger(tmp_path):
    services, _, _ = make_services(tmp_path)
    for extra in ({"approved": True}, {"sender": "1"}, {"url": "http://x"}, {"bogus": 1}):
        out = request(services, workspace="home", chat=CHAT_A, action="continue", **extra)
        assert not out["ok"] and out["error"]["code"] in ("forbidden_key", "unknown_key")
    assert Ledger(services.state_dir).read_all() == []


def test_envelope_entry_point(tmp_path):
    services, _, _ = make_services(tmp_path)
    assert api.dispatch_envelope(services, {"method": "requests.list"})["ok"]
    assert (
        codes(api.dispatch_envelope(services, {"method": "requests.list", "approved": True}))
        == "forbidden_key"
    )
    assert codes(api.dispatch_envelope(services, [1])) == "invalid_request"


def test_sessions_get_latest_turn_aliases_the_exact_rendered_turn(tmp_path):
    stale_continuation = row(CHAT_A, turn="turn-9", continuation_turn="turn-3")
    services, _, _ = make_services(tmp_path, combined({"home": [stale_continuation]}))
    out = call(services, "sessions.get", workspace="home", chat="alpha")["result"]
    assert out["session"]["latest_turn"] == out["session"]["turn"] == "turn-9"
    assert out["session"]["continuation_turn"] == "turn-3"  # existing metadata stays intact
    assert out["account"]["workspace"] == "home"


def test_tg_request_shape_with_bare_ids_passes_validate_and_dispatch(tmp_path):
    """The tg facade sends bare chat IDs; PM canonicalizes once and Guardian sees the URL."""
    services, _, _ = make_services(tmp_path)
    got = call(services, "sessions.get", workspace="home", chat="alpha")
    assert got["ok"] and got["result"]["session"]["chat"] == CHAT_A
    queued = request(services, workspace="home", chat="alpha", action="continue", reason="tg")
    assert queued["ok"], queued
    assert queued["result"]["chat"] == CHAT_A and queued["result"]["expected_turn"] == "turn-1"
    again = request(services, workspace="home", chat=CHAT_A, action="continue")
    assert again["result"]["request_id"] == queued["result"]["request_id"]
    assert again["result"]["duplicate"] is True
    paused = request(services, workspace="home", chat="alpha", action="pause")
    assert paused["ok"] and paused["result"]["chat"] == CHAT_A
    (created,) = [
        e
        for e in Ledger(services.state_dir).read_all()
        if e["request_id"] == queued["result"]["request_id"]
    ]
    assert created["chat"] == CHAT_A
