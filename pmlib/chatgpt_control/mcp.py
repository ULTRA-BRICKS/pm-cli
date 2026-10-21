"""Stdio MCP server (JSON-RPC 2.0, newline-delimited) over the one control API.

Accessed via: ``pm chatgpt mcp`` (and the fixed launcher the guardian connector installs).

Invariants: every tool is a mapping onto ``api.dispatch`` (``mcp_tools``), so there is one policy
and no tool can approve, send, or accept a recipient, URL or command; write tools only queue an
owner-approval request. Input is bounded (one line at most ``MAX_LINE_BYTES``; an oversized line
is answered with an error and discarded, never buffered), batches and non-object messages are
refused, unknown methods and tools fail closed, and nothing but JSON-RPC lines is ever written to
stdout. Exception text never leaves the process.
"""

from __future__ import annotations

import json

from .. import __version__
from . import api, mcp_tools
from .service import Services

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "pm-chatgpt-control"
MAX_LINE_BYTES = 1 << 20

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_INITIALIZED = -32002


class _Reject(Exception):
    def __init__(self, code: int, message: str, request_id: object = None) -> None:
        super().__init__(message)
        self.code, self.message, self.request_id = code, message, request_id


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_failure(code: str, message: str) -> dict:
    envelope = api.failure(code, message)
    return {
        "content": [{"type": "text", "text": json.dumps(envelope, separators=(",", ":"))}],
        "structuredContent": envelope,
        "isError": True,
    }


class Server:
    def __init__(self, services: Services) -> None:
        self.services = services
        self.initialized = False

    def handle(self, message: object) -> dict | None:
        """One decoded message in, at most one response out; ``None`` for a notification."""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "request must be a JSON object")
        has_id = "id" in message
        request_id = message.get("id")
        if has_id and (isinstance(request_id, bool) or not isinstance(request_id, str | int)):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(request_id if has_id else None, INVALID_REQUEST, "not a JSON-RPC request")
        method = message["method"]
        if not has_id:
            return None  # notifications (initialized, cancelled, ...) never get a reply
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        try:
            return {"jsonrpc": "2.0", "id": request_id, "result": self._dispatch(method, params)}
        except _Reject as exc:
            return _error(request_id, exc.code, exc.message)
        except Exception:  # noqa: BLE001 - fixed text: exception detail could echo paths/secrets
            return _error(request_id, INTERNAL_ERROR, "internal error")

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            self.initialized = True
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "Read tools report ChatGPT Guardian state. Request tools only queue an owner "
                    "approval in Telegram; nothing is sent or changed until the owner approves."
                ),
            }
        if method == "ping":
            return {}
        if method not in ("tools/list", "tools/call"):
            raise _Reject(METHOD_NOT_FOUND, "method not found")
        if not self.initialized:
            raise _Reject(NOT_INITIALIZED, "initialize first")
        if method == "tools/list":
            return {"tools": [tool.listing() for tool in mcp_tools.TOOLS]}
        name = params.get("name")
        if not isinstance(name, str) or name not in mcp_tools.BY_NAME:
            raise _Reject(INVALID_PARAMS, "unknown tool")
        tool = mcp_tools.BY_NAME[name]
        try:
            arguments = mcp_tools.validate_arguments(tool, params.get("arguments", {}))
        except ValueError as exc:
            text = str(exc)
            code = "forbidden_key" if text.startswith("forbidden_key") else "invalid_params"
            return _tool_failure(code, text[:200])
        return mcp_tools.call_tool(self.services, name, arguments)


def _read_line(stdin) -> tuple[str | None, bool]:
    """``(line, oversized)``; ``(None, False)`` at EOF. An oversized line is drained, not kept."""
    line = stdin.readline(MAX_LINE_BYTES + 1)
    if line == "":
        return None, False
    if len(line) > MAX_LINE_BYTES and not line.endswith("\n"):
        while True:
            chunk = stdin.readline(MAX_LINE_BYTES)
            if chunk == "" or chunk.endswith("\n"):
                break
        return "", True
    return line, False


def serve(services: Services, stdin, stdout) -> int:
    server = Server(services)
    while True:
        line, oversized = _read_line(stdin)
        if line is None:
            return 0
        if oversized:
            reply: dict | None = _error(None, INVALID_REQUEST, "message is too large")
        elif not line.strip():
            continue
        else:
            try:
                reply = server.handle(json.loads(line))
            except (ValueError, RecursionError):
                reply = _error(None, PARSE_ERROR, "parse error")
        if reply is not None:
            stdout.write(json.dumps(reply, separators=(",", ":"), ensure_ascii=True) + "\n")
            stdout.flush()
