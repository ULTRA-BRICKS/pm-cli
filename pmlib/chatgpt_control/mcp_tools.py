"""The MCP tool catalogue: names, schemas, annotations and the mapping onto the control API.

Accessed via: ``mcp`` (the stdio server) and ``connector`` (the manifest lists the names).

Invariants: every tool is a thin mapping onto ``api.dispatch`` (one policy, no second copy). Read
tools are read-only. Write tools only QUEUE an owner-approval request; there is no tool that
approves, sends, or accepts a Telegram sender, recipient, URL or command. Annotations describe
each tool's OWN effect: a request tool appends one local ledger record (not read-only, not
destructive, idempotent because duplicates return the original receipt, closed world); the
later owner-approved action is performed by the approval worker, not by the tool.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from ..chatgpt_monitor import ACCOUNTS
from . import api
from .schema import ACTIONS, CHAT_ACTIONS, FORBIDDEN_KEYS, MAX_CHAT, MAX_REASON, TURN
from .service import Services

CHAT_PATTERN = r"^(https://chatgpt\.com/c/[A-Za-z0-9_-]+/?|[A-Za-z0-9_-]{1,128})$"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
MAX_RESULT_TEXT = 1 << 20

READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
REQUEST_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

_WORKSPACE = {"type": "string", "enum": list(ACCOUNTS), "description": "Guardian account."}
_WORKSPACE_ALL = {
    "type": "string",
    "enum": [*ACCOUNTS, "all"],
    "description": "Guardian account or all accounts (default all).",
}
_CHAT = {
    "type": "string",
    "maxLength": MAX_CHAT,
    "pattern": CHAT_PATTERN,
    "description": "Bare conversation id or exact permanent https://chatgpt.com/c/<id> URL.",
}
_REASON = {
    "type": "string",
    "maxLength": MAX_REASON,
    "description": "Optional note shown to the owner on the approval card.",
}


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    annotations: dict
    handler: Callable[[Services, dict], dict]  # returns an API envelope

    def listing(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"title": self.title, **self.annotations},
        }


def _call(method: str, fixed: dict | None = None):
    def handler(services: Services, args: dict) -> dict:
        return api.dispatch(services, method, {**args, **(fixed or {})}, source="mcp")

    return handler


def _events(services: Services, args: dict) -> dict:
    envelope = api.dispatch(services, "sessions.list", args, source="mcp")
    if not envelope["ok"]:
        return envelope
    result = envelope["result"]
    health = [
        {"workspace": a.get("workspace"), "ok": a.get("ok"), "error": a.get("error")}
        for a in result["accounts"]
    ]
    return {
        "ok": True,
        "result": {
            "workspace": result["workspace"],
            "accounts": health,
            "events": result["events"],
        },
    }


_CONTROL_ACTIONS = [a for a in ACTIONS if a not in ("continue",)]

TOOLS: tuple[Tool, ...] = (
    Tool(
        "chatgpt_accounts",
        "List ChatGPT Guardian accounts",
        "Supported accounts, whether each is configured, its service state and observed counts.",
        _schema({}),
        READ_ANNOTATIONS,
        _call("accounts.list"),
    ),
    Tool(
        "chatgpt_sessions",
        "List watched ChatGPT sessions",
        "Current sessions, account health and recent events for one account or all accounts.",
        _schema({"workspace": _WORKSPACE_ALL}),
        READ_ANNOTATIONS,
        _call("sessions.list"),
    ),
    Tool(
        "chatgpt_session_get",
        "Get one ChatGPT session",
        "One current session with its account's health. Identity is account plus chat URL.",
        _schema({"workspace": _WORKSPACE, "chat": _CHAT}, ("workspace", "chat")),
        READ_ANNOTATIONS,
        _call("sessions.get"),
    ),
    Tool(
        "chatgpt_events",
        "List recent session events",
        "Recent observed events (baseline events are history, not new sends) with account health.",
        _schema({"workspace": _WORKSPACE_ALL}),
        READ_ANNOTATIONS,
        _events,
    ),
    Tool(
        "chatgpt_requests",
        "List approval requests",
        "Owner-approval requests and their current status, newest first.",
        _schema({}),
        READ_ANNOTATIONS,
        _call("requests.list"),
    ),
    Tool(
        "chatgpt_request_get",
        "Get an approval request receipt",
        "Status, binding fields and timeline of one approval request.",
        _schema(
            {
                "request_id": {
                    "type": "string",
                    "pattern": UUID_PATTERN,
                    "description": "Request UUID.",
                }
            },
            ("request_id",),
        ),
        READ_ANNOTATIONS,
        _call("requests.get"),
    ),
    Tool(
        "chatgpt_connectors",
        "List connector install state",
        "State of the fixed guardian/chatgpt connector and the prepared platform setup steps.",
        _schema({}),
        READ_ANNOTATIONS,
        _call("connectors.list"),
    ),
    Tool(
        "chatgpt_request_continue",
        "Request owner approval to continue a ChatGPT session",
        "Queue an owner approval to send the single fixed continuation message to one exact latest "
        "turn. This does NOT send anything: the owner must approve in Telegram, and the turn must "
        "still be unchanged, idle and unblocked when approved.",
        _schema(
            {
                "workspace": _WORKSPACE,
                "chat": _CHAT,
                "expected_turn": {
                    "type": "string",
                    "pattern": TURN.pattern,
                    "description": "Latest turn id you observed; defaults to the currently observed turn.",
                },
                "reason": _REASON,
            },
            ("workspace", "chat"),
        ),
        REQUEST_ANNOTATIONS,
        _call("actions.request", {"action": "continue"}),
    ),
    Tool(
        "chatgpt_request_control",
        "Request owner approval for a Guardian control",
        "Queue an owner approval for watch, unwatch, pause, resume, service-start or service-stop. "
        "This does NOT change anything by itself: the owner must approve in Telegram first.",
        _schema(
            {
                "workspace": _WORKSPACE,
                "action": {"type": "string", "enum": _CONTROL_ACTIONS},
                "chat": {
                    **_CHAT,
                    "description": _CHAT["description"] + " Omit for service actions.",
                },
                "reason": _REASON,
            },
            ("workspace", "action"),
        ),
        REQUEST_ANNOTATIONS,
        _call("actions.request"),
    ),
    Tool(
        "chatgpt_request_connector_install",
        "Request owner approval to install the guardian connector locally",
        "Queue an owner approval to install the fixed, reviewed local MCP launcher and manifest. "
        "ChatGPT workspace registration, OAuth and the platform risk warning stay with the owner.",
        _schema({}),
        REQUEST_ANNOTATIONS,
        _call("connectors.request", {"connector": "guardian", "target": "chatgpt"}),
    ),
)
TOOL_NAMES = tuple(t.name for t in TOOLS)
READ_TOOL_NAMES = tuple(t.name for t in TOOLS if t.annotations["readOnlyHint"])
REQUEST_TOOL_NAMES = tuple(t.name for t in TOOLS if not t.annotations["readOnlyHint"])
BY_NAME = {t.name: t for t in TOOLS}
assert set(CHAT_ACTIONS) <= set(ACTIONS)  # the enums above are derived from the API vocabulary


def validate_arguments(tool: Tool, args: object) -> dict:
    """Check ``args`` against the tool's advertised schema; ``ValueError`` carries a safe message."""
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")  # noqa: TRY004
    schema = tool.input_schema
    for key in args:
        if key in FORBIDDEN_KEYS:
            raise ValueError(
                f"forbidden_key: {key[:20]!r} is not accepted; requests carry no authority"
            )
        if key not in schema["properties"]:
            raise ValueError(f"unknown property {key[:20]!r}")
    for key in schema["required"]:
        if key not in args:
            raise ValueError(f"missing required property {key!r}")
    for key, value in args.items():
        spec = schema["properties"][key]
        if spec["type"] == "string" and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"{key} must be one of " + ", ".join(spec["enum"]))
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            raise ValueError(f"{key} is too long")
    return args


def call_tool(services: Services, name: str, args: dict) -> dict:
    """Run a validated tool call and wrap the API envelope as an MCP tool result."""
    envelope = BY_NAME[name].handler(services, args)
    text = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True)
    if len(text) > MAX_RESULT_TEXT:
        envelope = api.failure("internal_error", "the result is too large to return")
        text = json.dumps(envelope, separators=(",", ":"))
    payload = envelope["result"] if envelope["ok"] else envelope
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": not envelope["ok"],
    }
