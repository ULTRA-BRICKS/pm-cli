"""The stdio MCP server: protocol shape, annotations, request-only writes, hostile input."""

from __future__ import annotations

import io
import json

import pytest

from pmlib.chatgpt_control import mcp, mcp_tools
from pmlib.chatgpt_control.ledger import Ledger
from tests.control_support import CHAT_A, make_services


def run(services, *messages, raw=None):
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    stdin = io.StringIO("\n".join(lines) + "\n" if raw is None else raw)
    stdout = io.StringIO()
    assert mcp.serve(services, stdin, stdout) == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
READY = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def call(name, arguments, id_=7):
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


@pytest.fixture
def services(tmp_path):
    return make_services(tmp_path)[0]


def test_initialize_then_tools_list(services):
    out = run(services, INIT, READY, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(out) == 2  # the notification gets no reply
    init = out[0]["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    assert init["serverInfo"]["name"] == "pm-chatgpt-control"
    tools = out[1]["result"]["tools"]
    assert [t["name"] for t in tools] == list(mcp_tools.TOOL_NAMES)
    for tool in tools:
        assert tool["inputSchema"]["additionalProperties"] is False
        ann = tool["annotations"]
        assert set(ann) >= {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        assert ann["destructiveHint"] is False and ann["openWorldHint"] is False
    names = " ".join(t["name"] for t in tools)
    assert "approve" not in names and "send" not in names
    read = [t for t in tools if t["annotations"]["readOnlyHint"]]
    assert len(read) == len(mcp_tools.READ_TOOL_NAMES) >= 6


def test_unknown_protocol_version_negotiates_to_ours(services):
    msg = {**INIT, "params": {**INIT["params"], "protocolVersion": "1999-01-01"}}
    assert run(services, msg)[0]["result"]["protocolVersion"] == "2025-06-18"


def test_tools_need_initialize_first(services):
    out = run(services, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert out[0]["error"]["code"] == -32002


def test_ping_and_unknown_method(services):
    out = run(
        services,
        INIT,
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "sampling/createMessage"},
    )
    assert out[1]["result"] == {}
    assert out[2]["error"]["code"] == -32601 and out[3]["error"]["code"] == -32601


def test_read_tool_returns_structured_content(services):
    out = run(services, INIT, call("chatgpt_sessions", {"workspace": "home"}))
    result = out[1]["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["sessions"][0]["chat"] == CHAT_A
    assert json.loads(result["content"][0]["text"])["ok"] is True


def test_request_tool_only_queues(services):
    out = run(
        services, INIT, call("chatgpt_request_continue", {"workspace": "home", "chat": "alpha"})
    )
    result = out[1]["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["status"] == "queued"
    assert result["structuredContent"]["chat"] == CHAT_A
    kinds = [e["kind"] for e in Ledger(services.state_dir).read_all()]
    assert kinds == ["request_created"]


def test_connector_and_control_request_tools_only_queue(services):
    out = run(
        services,
        INIT,
        call("chatgpt_request_connector_install", {}, 2),
        call(
            "chatgpt_request_control", {"workspace": "home", "action": "pause", "chat": CHAT_A}, 3
        ),
    )
    assert out[1]["result"]["structuredContent"]["status"] == "queued"
    assert out[2]["result"]["structuredContent"]["status"] == "queued"
    kinds = {e["kind"] for e in Ledger(services.state_dir).read_all()}
    assert kinds == {"request_created"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"workspace": "home", "chat": "alpha", "approved": True},
        {"workspace": "home", "chat": "alpha", "sender": "1"},
        {"workspace": "home", "chat": "alpha", "extra": 1},
        {"workspace": "all", "chat": "alpha"},
        {"workspace": "home"},
        {"workspace": "home", "chat": "https://evil.example/c/x"},
        {"workspace": "home", "chat": "alpha", "expected_turn": "x" * 500},
        {"workspace": "home", "chat": 5},
        "string",
        [],
    ],
)
def test_hostile_arguments_are_errors_and_write_nothing(services, arguments):
    out = run(services, INIT, call("chatgpt_request_continue", arguments))
    assert out[1]["result"]["isError"] is True
    assert Ledger(services.state_dir).read_all() == []


def test_unknown_tool_is_a_protocol_error(services):
    out = run(services, INIT, call("chatgpt_approve", {}), call("shell", {"cmd": "id"}, 8))
    assert out[1]["error"]["code"] == -32602 and out[2]["error"]["code"] == -32602


def test_malformed_input_never_kills_the_server(services):
    out = run(
        services,
        "{not json",
        "[1,2,3]",
        "42",
        '{"jsonrpc":"1.0","id":1,"method":"ping"}',
        '{"jsonrpc":"2.0","id":true,"method":"ping"}',
        '{"jsonrpc":"2.0","id":1}',
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":"x"}',
        INIT,
        '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":"x"}',
        '{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":7}}',
        {"jsonrpc": "2.0", "id": 11, "method": "ping"},
    )
    codes = [m.get("error", {}).get("code") for m in out]
    assert codes[0] == -32700 and codes[1] == -32600 and codes[2] == -32600
    assert codes[3] == -32600 and codes[4] == -32600 and codes[5] == -32600
    assert out[-1]["result"] == {}  # still serving


def test_oversized_line_is_rejected_then_serving_continues(services, monkeypatch):
    monkeypatch.setattr(mcp, "MAX_LINE_BYTES", 2048)
    big = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {"x": "y" * 5000}})
    out = run(services, INIT, big, {"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert out[1]["error"]["code"] == -32600
    assert out[2]["result"] == {}


def test_tool_failure_text_never_leaks(services):
    def boom(_accounts):
        raise RuntimeError("Bearer SECRET at /home/ultra/.config")

    services.fetch = boom
    out = run(services, INIT, call("chatgpt_sessions", {}))
    blob = json.dumps(out[1])
    assert out[1]["result"]["isError"] is True and "SECRET" not in blob and "/home" not in blob


def test_stdout_carries_only_json_lines(services):
    stdout = io.StringIO()
    mcp.serve(services, io.StringIO(json.dumps(INIT) + "\nnot json\n"), stdout)
    for line in stdout.getvalue().splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"


def test_deeply_nested_json_is_a_parse_error_not_a_crash(services):
    out = run(services, "[" * 100000, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert out[0]["error"]["code"] == -32700 and out[1]["result"] == {}


@pytest.mark.parametrize(
    "value",
    [
        "a" * 128,
        "a" * 129,
        "https://chatgpt.com/c/" + "a" * 200,
        "https://chatgpt.com/c/",
        "a/b",
        "",
    ],
)
def test_advertised_chat_pattern_agrees_with_runtime_validation(value):
    import re

    from pmlib.chatgpt_control import schema
    from pmlib.chatgpt_control.errors import ApiError

    advertised = re.fullmatch(mcp_tools.CHAT_PATTERN, value) is not None
    advertised = advertised and len(value) <= mcp_tools._CHAT["maxLength"]
    try:
        schema.parse_chat(value)
        accepted = True
    except ApiError:
        accepted = False
    assert advertised == accepted
