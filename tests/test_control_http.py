"""Loopback HTTP API: token handling, auth before anything else, caps, request-only writes."""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import threading

import pytest

from pmlib.chatgpt_control import http_api
from pmlib.chatgpt_control.ledger import Ledger
from tests.control_support import make_services

TOKEN = "t" * 43


@pytest.fixture
def server(tmp_path):
    services, _, _ = make_services(tmp_path)
    httpd = http_api.make_server(services, 0, TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, services
    httpd.shutdown()
    httpd.server_close()
    thread.join(5)


def request(httpd, method, path, body=None, headers=None, token=TOKEN):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    hdrs = dict(headers or {})
    if token is not None:
        hdrs.setdefault("Authorization", f"Bearer {token}")
    if isinstance(body, dict | list):
        body = json.dumps(body)
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, raw


def rpc(httpd, method, **params):
    return request(httpd, "POST", "/v1/rpc", {"method": method, "params": params})


def test_authenticated_read_and_queue_only_write(server):
    httpd, services = server
    status, body = rpc(httpd, "accounts.list")
    assert status == 200 and body["ok"] is True
    status, body = rpc(httpd, "actions.request", workspace="home", chat="alpha", action="continue")
    assert status == 200 and body["result"]["status"] == "queued"
    assert [e["kind"] for e in Ledger(services.state_dir).read_all()] == ["request_created"]


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN[:-1], TOKEN + "x"])
def test_every_route_needs_the_token_before_anything_else(server, token):
    httpd, services = server
    for method, path in (
        ("POST", "/v1/rpc"),
        ("GET", "/v1/health"),
        ("GET", "/"),
        ("PUT", "/v1/rpc"),
    ):
        status, body = request(
            httpd,
            method,
            path,
            {"method": "requests.list"},
            token=token,
            headers={"Authorization": f"Bearer {token}"} if token is not None else None,
        )
        assert status == 401 and body["error"]["code"] == "unauthorized"
    assert Ledger(services.state_dir).read_all() == []


def test_non_bearer_schemes_are_rejected(server):
    httpd, _ = server
    for value in (TOKEN, f"Basic {TOKEN}", f"bearer {TOKEN}x", f"Bearer  {TOKEN}"):
        status, _ = request(
            httpd, "GET", "/v1/health", headers={"Authorization": value}, token=None
        )
        assert status == 401


def test_health_is_authenticated_and_minimal(server):
    httpd, _ = server
    status, body = request(httpd, "GET", "/v1/health")
    assert status == 200 and body == {"ok": True, "result": {"status": "ok"}}


def test_unknown_paths_and_methods(server):
    httpd, _ = server
    assert request(httpd, "GET", "/v1/rpc")[0] == 405
    assert request(httpd, "POST", "/v1/other", {"method": "x"})[0] == 404
    assert request(httpd, "DELETE", "/v1/rpc")[0] == 405


def test_body_caps_and_framing(server):
    httpd, services = server
    status, body = request(httpd, "POST", "/v1/rpc", "x" * (http_api.MAX_BODY_BYTES + 1))
    assert status == 413 and body["error"]["code"] == "payload_too_large"
    status, _ = request(httpd, "POST", "/v1/rpc", "{not json")
    assert status == 400
    status, body = request(httpd, "POST", "/v1/rpc", "[1]")
    assert status == 400 and body["error"]["code"] == "invalid_request"
    status, _ = request(
        httpd,
        "POST",
        "/v1/rpc",
        {"method": "accounts.list"},
        headers={"Content-Type": "text/plain"},
    )
    assert status == 415
    raw = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
    raw.sendall(
        f"POST /v1/rpc HTTP/1.1\r\nHost: 127.0.0.1:{httpd.server_address[1]}\r\nAuthorization: Bearer {TOKEN}\r\n"
        "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n".encode()
    )
    assert raw.recv(64).split(b" ")[1] in (b"411", b"400", b"501")
    raw.close()
    assert Ledger(services.state_dir).read_all() == []


def test_authority_keys_are_refused_over_http(server):
    httpd, services = server
    _, body = rpc(
        httpd, "actions.request", workspace="home", chat="alpha", action="continue", approved=True
    )
    assert body["ok"] is False and body["error"]["code"] == "forbidden_key"
    assert Ledger(services.state_dir).read_all() == []
    _, body = request(httpd, "POST", "/v1/rpc", {"method": "requests.list", "approved": True})
    assert body["error"]["code"] == "forbidden_key"


def test_browser_and_rebinding_requests_are_refused(server):
    httpd, _ = server
    status, _ = request(
        httpd,
        "POST",
        "/v1/rpc",
        {"method": "accounts.list"},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403
    status, _ = request(
        httpd, "POST", "/v1/rpc", {"method": "accounts.list"}, headers={"Host": "evil.example"}
    )
    assert status == 403
    port = httpd.server_address[1]
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        assert request(httpd, "GET", "/v1/health", headers={"Host": host})[0] == 200


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "", "::", "192.168.1.5", "example.com", "localhost.evil"]
)
def test_only_loopback_binds(tmp_path, host):
    services, _, _ = make_services(tmp_path)
    with pytest.raises(ValueError):
        http_api.make_server(services, 0, TOKEN, host=host)


def test_short_token_is_refused(tmp_path):
    services, _, _ = make_services(tmp_path)
    with pytest.raises(ValueError):
        http_api.make_server(services, 0, "short")


def test_connection_cap_and_read_timeout(tmp_path):
    services, _, _ = make_services(tmp_path)
    httpd = http_api.make_server(services, 0, TOKEN, max_connections=1, io_timeout=2)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        idle = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
        status, body = request(httpd, "GET", "/v1/health")
        assert status == 503 and body["error"]["code"] == "busy"
        assert idle.recv(1) == b""  # the stalled client is cut off by the read timeout
        idle.close()
        assert request(httpd, "GET", "/v1/health")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def test_internal_failures_do_not_leak(server):
    httpd, services = server

    def boom(_accounts):
        raise RuntimeError("Bearer SECRET")

    services.fetch = boom
    _, body = rpc(httpd, "sessions.list")
    assert "SECRET" not in json.dumps(body) and body["error"]["code"] == "internal_error"


class TestToken:
    def test_init_creates_private_file_and_never_returns_the_secret(self, tmp_path):
        path = http_api.init_token(tmp_path / "state")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        token = http_api.load_token(tmp_path / "state")
        assert len(token) >= 32 and token not in str(path)

    def test_init_refuses_to_overwrite_without_rotate(self, tmp_path):
        http_api.init_token(tmp_path)
        first = http_api.load_token(tmp_path)
        with pytest.raises(http_api.TokenError):
            http_api.init_token(tmp_path)
        assert http_api.load_token(tmp_path) == first
        http_api.init_token(tmp_path, rotate=True)
        assert http_api.load_token(tmp_path) != first

    def test_load_rejects_missing_loose_symlinked_and_short_tokens(self, tmp_path):
        with pytest.raises(http_api.TokenError):
            http_api.load_token(tmp_path)
        path = http_api.init_token(tmp_path)
        os.chmod(path, 0o644)
        with pytest.raises(http_api.TokenError):
            http_api.load_token(tmp_path)
        os.chmod(path, 0o600)
        path.write_text("short\n")
        with pytest.raises(http_api.TokenError):
            http_api.load_token(tmp_path)
        real = tmp_path / "elsewhere"
        real.write_text("z" * 43 + "\n")
        os.chmod(real, 0o600)
        path.unlink()
        path.symlink_to(real)
        with pytest.raises(http_api.TokenError):
            http_api.load_token(tmp_path)
