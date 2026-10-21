"""Account-local Guardian client: exact POST /continue body, per-account tokens, honest failures.

A stdlib loopback HTTP server plays Guardian; no real Guardian and no real token is touched.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pmlib.chatgpt_control.guardian_client import (
    ContinueReply,
    GuardianClient,
    GuardianConfig,
    GuardianUnavailable,
    GuardianUncertain,
)

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
BODY = {
    "request_id": UUID,
    "chat": "https://chatgpt.com/c/alpha",
    "expected_turn": "turn-1",
    "approved_at": 1792254000,
    "expires_at": 1792254000 + 900,
}
TOKENS = {"home": "h" * 40, "glide.vc": "g" * 40}


class FakeGuardian:
    def __init__(self, account):
        self.account = account
        self.requests = []
        self.post = (202, {"ok": True, "request_id": UUID, "status": "queued"})
        self.get = (200, {"request_id": UUID, "status": "sent", "reason": ""})
        self.delay = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, payload):
                time.sleep(outer.delay)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
                self._reply(*outer.post)

            def do_GET(self):
                outer.requests.append(("GET", self.path, self.headers.get("Authorization"), b""))
                self._reply(*outer.get)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def world(tmp_path):
    """A fake HOME with Guardian workspace config for both accounts and two live fake servers."""
    servers = {a: FakeGuardian(a) for a in TOKENS}
    config_dir = tmp_path / ".config/agent-tools/chatgpt-guardian"
    config_dir.mkdir(parents=True)
    for account, token in TOKENS.items():
        state_dir = tmp_path / ".local/state/guardian" / account
        state_dir.mkdir(parents=True)
        (state_dir / "state.sqlite").write_text("")
        token_file = state_dir / "control.token"
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)
        (config_dir / f"{account}.json").write_text(
            json.dumps({"workspace": account, "state": str(state_dir / "state.sqlite")})
        )
    ports = {a: s.port for a, s in servers.items()}
    config = GuardianConfig(home=tmp_path, ports=ports)
    yield tmp_path, servers, GuardianClient(config, timeout=2.0)
    for server in servers.values():
        server.close()


def test_exact_body_and_account_local_bearer(world):
    _, servers, client = world
    reply = client.request_continue("glide.vc", BODY)
    assert reply == ContinueReply("queued", "")
    method, path, auth, raw = servers["glide.vc"].requests[0]
    assert (method, path, auth) == ("POST", "/continue", "Bearer " + TOKENS["glide.vc"])
    assert json.loads(raw) == BODY and set(json.loads(raw)) == set(BODY)
    assert servers["home"].requests == []  # one account's call never reaches or uses the other


def test_home_uses_only_the_home_token(world):
    _, servers, client = world
    client.request_continue("home", BODY)
    assert servers["home"].requests[0][2] == "Bearer " + TOKENS["home"]
    assert servers["glide.vc"].requests == []


@pytest.mark.parametrize(
    "mutate",
    [
        {"extra": 1},
        {"request_id": "not-a-uuid"},
        {"approved_at": "1"},
        {"approved_at": True},
        {"expires_at": 1792254000 + 901},
        {"expires_at": 1792254000 - 1},
        {"chat": "https://evil.example/c/x"},
        {"expected_turn": ""},
    ],
)
def test_malformed_bodies_never_leave_the_process(world, mutate):
    _, servers, client = world
    with pytest.raises(ValueError):
        client.request_continue("home", {**BODY, **mutate})
    assert servers["home"].requests == []


def test_missing_key_is_refused_before_sending(world):
    _, servers, client = world
    body = dict(BODY)
    del body["expected_turn"]
    with pytest.raises(ValueError):
        client.request_continue("home", body)
    assert servers["home"].requests == []


def test_unknown_account_is_refused(world):
    with pytest.raises(ValueError):
        world[2].request_continue("work", BODY)


def test_status_query(world):
    _, servers, client = world
    status = client.continuation("home", UUID)
    assert (status.state, status.detail) == ("sent", "")
    assert servers["home"].requests[0][:3] == (
        "GET",
        f"/continuations/{UUID}",
        "Bearer " + TOKENS["home"],
    )


@pytest.mark.parametrize("state", ["queued", "refused", "sent", "uncertain"])
def test_all_documented_states(world, state):
    _, servers, client = world
    servers["home"].get = (200, {"request_id": UUID, "status": state, "reason": "why"})
    assert client.continuation("home", UUID).state == state


def test_unknown_or_mismatched_status_is_not_trusted(world):
    _, servers, client = world
    servers["home"].get = (200, {"request_id": UUID, "status": "delivered"})
    assert client.continuation("home", UUID).state is None
    servers["home"].get = (
        200,
        {"request_id": "0f8fad5b-d9cb-469f-a165-70867728950f", "status": "sent"},
    )
    assert client.continuation("home", UUID).state is None
    servers["home"].get = (404, {"error": "unknown"})
    assert client.continuation("home", UUID).state is None
    with pytest.raises(ValueError):
        client.continuation("home", "../../etc")


def test_refused_by_guardian_is_a_refusal_not_a_failure(world):
    _, servers, client = world
    servers["home"].post = (400, {"error": "Invalid control request"})
    assert client.request_continue("home", BODY) == ContinueReply(
        "refused", "Invalid control request"
    )


@pytest.mark.parametrize("status", [404, 405, 501])
def test_route_not_installed_is_clean_unavailable(world, status):
    _, servers, client = world
    servers["home"].post = (status, {"error": "Unknown route"})
    with pytest.raises(GuardianUnavailable) as exc:
        client.request_continue("home", BODY)
    assert "guardian continuation unavailable" in str(exc.value)


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_rejected_is_unavailable_without_the_token(world, status):
    _, servers, client = world
    servers["home"].post = (status, {"error": "Authentication required"})
    with pytest.raises(GuardianUnavailable) as exc:
        client.request_continue("home", BODY)
    assert TOKENS["home"] not in str(exc.value) and "guardian continuation unavailable" in str(
        exc.value
    )


def test_connection_refused_is_unavailable(tmp_path, world):
    root, _, _ = world
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    client = GuardianClient(
        GuardianConfig(home=root, ports={"home": closed, "glide.vc": closed}), timeout=1
    )
    with pytest.raises(GuardianUnavailable) as exc:
        client.request_continue("home", BODY)
    assert "guardian continuation unavailable" in str(exc.value)


@pytest.mark.parametrize(
    "reply",
    [
        (500, {"error": "boom"}),
        (202, b"not json"),
        (202, {"ok": True, "request_id": UUID, "status": "sent"}),
        (202, {"ok": False, "request_id": UUID, "status": "queued"}),
        (
            202,
            {"ok": True, "request_id": "0f8fad5b-d9cb-469f-a165-70867728950f", "status": "queued"},
        ),
        (200, {"ok": True, "request_id": UUID, "status": "queued"}),
    ],
)
def test_ambiguous_replies_are_uncertain_never_retried(world, reply):
    _, servers, client = world
    servers["home"].post = reply
    with pytest.raises(GuardianUncertain):
        client.request_continue("home", BODY)
    assert len(servers["home"].requests) == 1


def test_slow_guardian_is_uncertain(world):
    _, servers, _ = world
    servers["home"].delay = 1.5
    client = GuardianClient(world[2]._config, timeout=0.3)
    with pytest.raises(GuardianUncertain):
        client.request_continue("home", BODY)
    assert len(servers["home"].requests) == 1


def test_oversized_reply_is_uncertain(world):
    _, servers, client = world
    servers["home"].post = (202, b"x" * 200000)
    with pytest.raises(GuardianUncertain):
        client.request_continue("home", BODY)


def test_token_file_rules(world):
    root, _, client = world
    token_file = root / ".local/state/guardian/home/control.token"
    token_file.chmod(0o644)
    with pytest.raises(GuardianUnavailable) as exc:
        client.request_continue("home", BODY)
    assert "0600" in str(exc.value) and TOKENS["home"] not in str(exc.value)
    token_file.chmod(0o600)
    token_file.write_text("short\n")
    with pytest.raises(GuardianUnavailable):
        client.request_continue("home", BODY)
    token_file.unlink()
    elsewhere = root / "elsewhere.token"
    elsewhere.write_text(TOKENS["home"])
    elsewhere.chmod(0o600)
    token_file.symlink_to(elsewhere)
    with pytest.raises(GuardianUnavailable):
        client.request_continue("home", BODY)


def test_config_must_name_the_same_account_and_an_absolute_state(world):
    root, servers, client = world
    config = root / ".config/agent-tools/chatgpt-guardian/home.json"
    config.write_text(json.dumps({"workspace": "glide.vc", "state": "/x/state.sqlite"}))
    with pytest.raises(GuardianUnavailable):
        client.request_continue("home", BODY)
    config.write_text(json.dumps({"workspace": "home", "state": "relative.sqlite"}))
    with pytest.raises(GuardianUnavailable):
        client.request_continue("home", BODY)
    config.write_text("not json")
    with pytest.raises(GuardianUnavailable):
        client.request_continue("home", BODY)
    config.unlink()
    with pytest.raises(GuardianUnavailable):
        client.request_continue("home", BODY)
    assert servers["home"].requests == []


def test_repr_never_contains_a_token(world):
    _, _, client = world
    client.request_continue("home", BODY)
    assert TOKENS["home"] not in repr(client) and TOKENS["home"] not in repr(client._config)
