"""External JSON can only REQUEST: exact keys, strict types, no authority-bearing fields."""

from __future__ import annotations

import pytest

from pmlib.chatgpt_control.errors import ApiError
from pmlib.chatgpt_control.schema import METHODS, parse_envelope, validate_params

CHAT = "https://chatgpt.com/c/alpha"
UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def code(method, params):
    with pytest.raises(ApiError) as exc:
        validate_params(method, params)
    return exc.value.code


def test_method_catalogue_is_the_frozen_set():
    assert set(METHODS) == {
        "accounts.list",
        "sessions.list",
        "sessions.get",
        "actions.request",
        "requests.list",
        "requests.get",
        "connectors.list",
        "connectors.request",
    }


@pytest.mark.parametrize("method", ["accounts.list", "requests.list", "connectors.list"])
def test_parameterless_methods_reject_any_key(method):
    assert validate_params(method, {}) == {}
    assert code(method, {"workspace": "home"}) == "unknown_key"


@pytest.mark.parametrize(
    "key",
    ["approved", "approved_at", "sender", "user_id", "recipient", "url", "command"],
)
@pytest.mark.parametrize("method", sorted(METHODS))
def test_authority_keys_are_forbidden_everywhere(method, key):
    base = {
        "accounts.list": {},
        "sessions.list": {},
        "sessions.get": {"workspace": "home", "chat": CHAT},
        "actions.request": {"workspace": "home", "action": "pause", "chat": CHAT},
        "requests.list": {},
        "requests.get": {"request_id": UUID},
        "connectors.list": {},
        "connectors.request": {"connector": "guardian", "target": "chatgpt"},
    }[method]
    assert code(method, {**base, key: True}) == "forbidden_key"
    assert code(method, {**base, key: "x"}) == "forbidden_key"


def test_unknown_keys_fail_closed():
    assert code("sessions.list", {"workspce": "home"}) == "unknown_key"


def test_sessions_list_workspace():
    assert validate_params("sessions.list", {}) == {"workspace": "all"}
    assert validate_params("sessions.list", {"workspace": "glide.vc"}) == {"workspace": "glide.vc"}
    assert code("sessions.list", {"workspace": "work"}) == "invalid_params"
    assert code("sessions.list", {"workspace": ["home"]}) == "invalid_params"


def test_sessions_get_requires_single_account_and_exact_chat():
    ok = validate_params("sessions.get", {"workspace": "home", "chat": CHAT + "/"})
    assert ok == {"workspace": "home", "chat": CHAT}
    assert code("sessions.get", {"workspace": "all", "chat": CHAT}) == "invalid_params"
    assert code("sessions.get", {"workspace": "home"}) == "invalid_params"
    for bad in (
        CHAT + "?x=1",
        CHAT + "#f",
        "https://evil.example/c/a",
        "http://chatgpt.com/c/a",
        "*",
        "",
    ):
        assert code("sessions.get", {"workspace": "home", "chat": bad}) == "invalid_params"


@pytest.mark.parametrize(
    "action",
    ["watch", "unwatch", "pause", "resume", "continue"],
)
def test_chat_actions_need_chat(action):
    params = {"workspace": "home", "action": action}
    if action == "continue":
        params["expected_turn"] = "turn-1"
    assert code("actions.request", params) == "invalid_params"
    assert validate_params("actions.request", {**params, "chat": CHAT})["chat"] == CHAT


@pytest.mark.parametrize("action", ["service-start", "service-stop"])
def test_service_actions_take_no_chat(action):
    assert validate_params("actions.request", {"workspace": "glide.vc", "action": action})
    assert (
        code("actions.request", {"workspace": "glide.vc", "action": action, "chat": CHAT})
        == "invalid_params"
    )


def test_all_workspace_mutations_fail_closed():
    for action in ("service-stop", "pause", "continue"):
        assert (
            code("actions.request", {"workspace": "all", "action": action, "chat": CHAT})
            == "invalid_params"
        )


def test_unknown_or_unsafe_actions_rejected():
    for action in ("sol-opt-in", "quota-clear", "send", "go", "rm -rf", "", None, 3):
        assert (
            code("actions.request", {"workspace": "home", "action": action, "chat": CHAT})
            == "invalid_params"
        )


def test_expected_turn_only_for_continue_and_well_formed():
    base = {"workspace": "home", "chat": CHAT}
    assert (
        code("actions.request", {**base, "action": "pause", "expected_turn": "t1"})
        == "invalid_params"
    )
    ok = validate_params(
        "actions.request", {**base, "action": "continue", "expected_turn": "0040f273-dc7a"}
    )
    assert ok["expected_turn"] == "0040f273-dc7a"
    for bad in ("", "a b", "x" * 200, "../x", "turn\n1", 5):
        assert (
            code("actions.request", {**base, "action": "continue", "expected_turn": bad})
            == "invalid_params"
        )


def test_continue_may_omit_expected_turn_for_server_to_bind():
    out = validate_params(
        "actions.request", {"workspace": "home", "chat": CHAT, "action": "continue"}
    )
    assert out["expected_turn"] is None


def test_reason_is_bounded_printable_text():
    base = {"workspace": "home", "chat": CHAT, "action": "pause"}
    assert (
        validate_params("actions.request", {**base, "reason": "needs a look"})["reason"]
        == "needs a look"
    )
    for bad in ("x" * 201, "line\nbreak", "esc\x1b[31m", "bidi\u202e", 5):
        assert code("actions.request", {**base, "reason": bad}) == "invalid_params"


def test_request_id_must_be_a_canonical_uuid():
    assert validate_params("requests.get", {"request_id": UUID}) == {"request_id": UUID}
    for bad in (UUID.upper(), UUID.replace("-", ""), "../etc/passwd", "", 7, UUID + "0"):
        assert code("requests.get", {"request_id": bad}) == "invalid_params"


def test_connector_request_is_the_fixed_reviewed_plan_only():
    assert validate_params("connectors.request", {"connector": "guardian", "target": "chatgpt"})
    for params in (
        {"connector": "guardian", "target": "claude"},
        {"connector": "shell", "target": "chatgpt"},
        {"connector": "guardian"},
        {"connector": "guardian", "target": "chatgpt", "package": "evil"},
    ):
        assert code("connectors.request", params) in ("invalid_params", "unknown_key")


def test_non_object_params_rejected():
    for params in ([], "x", 1, None, True):
        assert code("accounts.list", params) == "invalid_params"


def test_envelope_exact_shape():
    assert parse_envelope({"method": "accounts.list"}) == ("accounts.list", {})
    assert parse_envelope({"method": "sessions.list", "params": {}}) == ("sessions.list", {})
    for bad in ([], "x", {}, {"method": 5}, {"method": "a", "params": []}):
        with pytest.raises(ApiError) as exc:
            parse_envelope(bad)
        assert exc.value.code == "invalid_request"
    with pytest.raises(ApiError) as exc:
        parse_envelope({"method": "accounts.list", "approved": True})
    assert exc.value.code == "forbidden_key"
    with pytest.raises(ApiError) as exc:
        parse_envelope({"method": "accounts.list", "extra": 1})
    assert exc.value.code == "unknown_key"


def test_unknown_method_fails_closed():
    assert code("shell.exec", {}) == "unknown_method"
    assert code("accounts.list ", {}) == "unknown_method"


@pytest.mark.parametrize("chat", ["alpha", "https://chatgpt.com/c/alpha", CHAT + "/"])
def test_bare_or_canonical_chat_normalizes_once(chat):
    assert validate_params("sessions.get", {"workspace": "home", "chat": chat})["chat"] == CHAT
    assert (
        validate_params("actions.request", {"workspace": "home", "chat": chat, "action": "pause"})[
            "chat"
        ]
        == CHAT
    )


@pytest.mark.parametrize(
    "chat",
    [
        "alpha/",
        "alpha beta",
        " alpha",
        "alpha\n",
        "alpha?x=1",
        "alpha#f",
        "a/b",
        "../etc",
        "%2e%2e",
        "c/alpha",
        "/c/alpha",
        "chatgpt.com/c/alpha",
        "https://chatgpt.com/c/alpha\n",
        "https://chatgpt.com/c/alpha/extra",
        "https://chatgpt.com.evil.example/c/alpha",
        "https://chatgpt.com@evil.example/c/alpha",
        "https://user@chatgpt.com/c/alpha",
        "https://chatgpt.com:443/c/alpha",
        "https://chatgpt.com/c/alpha?x=1",
        "https://chatgpt.com/c/alpha#x",
        "HTTPS://chatgpt.com/c/alpha",
        "https://chatgpt.com/c/",
        "\u0430lpha",
        "a" * 3000,
        None,
        7,
        ["alpha"],
    ],
)
def test_chat_tricks_are_rejected(chat):
    assert code("sessions.get", {"workspace": "home", "chat": chat}) == "invalid_params"


def test_bare_id_charset_is_preserved():
    got = validate_params("sessions.get", {"workspace": "home", "chat": "A_b-9"})
    assert got["chat"] == "https://chatgpt.com/c/A_b-9"
