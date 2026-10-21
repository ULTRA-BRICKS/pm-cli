"""The one dispatch function behind ``pm chatgpt api``, the HTTP server and the MCP tools.

Accessed via: ``commands/chatgpt.py`` (CLI), ``http_api`` (``POST /v1/rpc``), ``mcp`` (tools).

Invariants: every call returns exactly one envelope, ``{ok:true,result}`` or
``{ok:false,error:{code,message}}``, and never raises for caller input. Reads return telemetry
and receipts; writes only QUEUE an owner-approval request (``service``). Nothing here approves,
asks Telegram, contacts Guardian or applies a control: that is the approval worker's job, behind
a verified owner tap. Unexpected failures surface as ``internal_error`` with a fixed message so
exception text (which could echo paths or credentials) never leaves the process.
"""

from __future__ import annotations

from collections.abc import Callable

from . import service
from .errors import ApiError
from .schema import parse_envelope, validate_params
from .service import Services

_ACTION_RESULT_KEYS = (
    "request_id",
    "status",
    "action",
    "workspace",
    "chat",
    "expected_turn",
    "expires_at",
    "plan_digest",
    "duplicate",
)


def _accounts_list(services: Services, params: dict, source: str) -> dict:
    return services.accounts()


def _sessions_list(services: Services, params: dict, source: str) -> dict:
    from ..chatgpt_monitor import ACCOUNTS

    workspace = params["workspace"]
    data = services.fetch(ACCOUNTS if workspace == "all" else (workspace,))
    return {
        "workspace": workspace,
        "accounts": data["accounts"],
        "sessions": data["sessions"],
        "events": data["events"],
    }


def _sessions_get(services: Services, params: dict, source: str) -> dict:
    workspace, chat = params["workspace"], params["chat"]
    data = services.fetch((workspace,))
    entries = [a for a in data["accounts"] if a.get("workspace") == workspace]
    if not entries or entries[0].get("ok") is not True:
        raise ApiError("account_unavailable", f"telemetry for {workspace} is unavailable")
    rows = [
        r
        for r in data["sessions"]
        if r.get("account") == workspace
        and isinstance(r.get("chat"), str)
        and r["chat"].rstrip("/") == chat
    ]
    if not rows:
        raise ApiError("not_found", "that chat is not in this account's current session list")
    if len(rows) > 1:
        raise ApiError(
            "refused", "this account lists that chat more than once; identity is ambiguous"
        )
    session = {
        **rows[0],
        "latest_turn": rows[0].get("turn"),
    }  # the exact rendered turn, not history
    return {"workspace": workspace, "account": entries[0], "session": session}


def _actions_request(services: Services, params: dict, source: str) -> dict:
    receipt = service.submit_action(services, params, source)
    return {key: receipt[key] for key in _ACTION_RESULT_KEYS}


def _connectors_list(services: Services, params: dict, source: str) -> dict:
    from . import connector

    return connector.list_connectors(services)


def _connectors_request(services: Services, params: dict, source: str) -> dict:
    receipt = service.submit_connector(services, params, source)
    return {
        **{key: receipt[key] for key in _ACTION_RESULT_KEYS},
        "connector": params["connector"],
        "target": params["target"],
    }


def _requests_list(services: Services, params: dict, source: str) -> dict:
    return service.list_requests(services)


def _requests_get(services: Services, params: dict, source: str) -> dict:
    return service.get_request(services, params["request_id"])


HANDLERS: dict[str, Callable[[Services, dict, str], dict]] = {
    "accounts.list": _accounts_list,
    "sessions.list": _sessions_list,
    "sessions.get": _sessions_get,
    "actions.request": _actions_request,
    "requests.list": _requests_list,
    "requests.get": _requests_get,
    "connectors.list": _connectors_list,
    "connectors.request": _connectors_request,
}


def failure(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def dispatch(services: Services, method: object, params: object, *, source: str = "api") -> dict:
    try:
        normalized = validate_params(method, params)
        return {"ok": True, "result": HANDLERS[method](services, normalized, source)}
    except ApiError as exc:
        return failure(exc.code, exc.message)
    except Exception:  # noqa: BLE001 - fixed message: exception text may echo paths or secrets
        return failure("internal_error", "the request failed unexpectedly; no state was confirmed")


def dispatch_envelope(services: Services, obj: object, *, source: str = "api") -> dict:
    try:
        method, params = parse_envelope(obj)
    except ApiError as exc:
        return failure(exc.code, exc.message)
    return dispatch(services, method, params, source=source)
