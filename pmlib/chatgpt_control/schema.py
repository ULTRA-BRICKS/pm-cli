"""Strict request validation for the frozen control API.

Accessed via: ``api.dispatch`` before any method runs, for CLI, HTTP and MCP alike.

Invariants: external JSON can only REQUEST. Authority-bearing keys (an ``approved`` flag, a
Telegram ``sender``/``user_id``, a ``recipient``, a ``url``, a ``command``) are refused with
``forbidden_key`` wherever they appear; any other undefined key is ``unknown_key``. Values are
type-exact (``bool`` is never an ``int``), bounded, and free of control characters.
"""

from __future__ import annotations

import re
import unicodedata
import uuid

from ..chatgpt_monitor import ACCOUNTS, CHAT_URL
from .errors import ApiError

FORBIDDEN_KEYS = frozenset(
    {
        "approved",
        "approved_at",
        "sender",
        "user_id",
        "recipient",
        "url",
        "command",
        "approval",
        "verdict",
        "token",
        "argv",
        "shell",
        "owner",
        "callback_data",
    }
)
CHAT_ACTIONS = ("watch", "unwatch", "pause", "resume", "continue")
SERVICE_ACTIONS = ("service-start", "service-stop")
ACTIONS = (*CHAT_ACTIONS, *SERVICE_ACTIONS)
CONNECTOR = "guardian"
CONNECTOR_TARGET = "chatgpt"
MAX_CHAT = 2048
CANONICAL_PREFIX = "https://chatgpt.com/c/"
BARE_CHAT = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_REASON = 200
TURN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
METHODS = (
    "accounts.list",
    "sessions.list",
    "sessions.get",
    "actions.request",
    "requests.list",
    "requests.get",
    "connectors.list",
    "connectors.request",
)


def clean_text(value: object, limit: int) -> str:
    """Untrusted display text (page titles, reasons): control/format characters removed, bounded."""
    return "".join(c for c in str(value) if unicodedata.category(c)[0] != "C")[:limit]


def check_keys(obj: dict, allowed: tuple[str, ...]) -> None:
    for key in obj:
        if key in FORBIDDEN_KEYS:
            raise ApiError(
                "forbidden_key", f"key {_shown(key)} is not accepted: requests carry no authority"
            )
    for key in obj:
        if key not in allowed:
            raise ApiError("unknown_key", f"unknown key {_shown(key)}")


def _shown(value: object) -> str:
    text = str(value)[:40]
    return repr("".join(c for c in text if unicodedata.category(c)[0] != "C"))


def _bad(message: str) -> ApiError:
    return ApiError("invalid_params", message)


def parse_workspace(value: object, *, allow_all: bool) -> str:
    choices = (*ACCOUNTS, "all") if allow_all else ACCOUNTS
    if not isinstance(value, str) or value not in choices:
        raise _bad("workspace must be one of " + ", ".join(choices))
    return value


def parse_chat(value: object) -> str:
    """Accept a bare chat id or the exact permanent URL; return the canonical URL (no slash)."""
    if isinstance(value, str) and len(value) <= MAX_CHAT:
        if BARE_CHAT.fullmatch(value) is not None:
            return CANONICAL_PREFIX + value
        if CHAT_URL.fullmatch(value) is not None:
            return value.rstrip("/")
    raise _bad(
        "chat must be a bare conversation id ([A-Za-z0-9_-]) or the exact permanent "
        "https://chatgpt.com/c/<id> URL"
    )


def parse_turn(value: object) -> str:
    if not isinstance(value, str) or TURN.fullmatch(value) is None:
        raise _bad("expected_turn must be 1-128 characters of [A-Za-z0-9_.:-]")
    return value


def parse_reason(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_REASON
        or any(unicodedata.category(c)[0] == "C" for c in value)
    ):
        raise _bad(f"reason must be printable text of at most {MAX_REASON} characters")
    return value


def parse_request_id(value: object) -> str:
    try:
        ok = isinstance(value, str) and str(uuid.UUID(value)) == value and len(value) == 36
    except ValueError:
        ok = False
    if not ok:
        raise _bad("request_id must be a canonical lowercase UUID")
    return value


def _no_params(params: dict) -> dict:
    check_keys(params, ())
    return {}


def _sessions_list(params: dict) -> dict:
    check_keys(params, ("workspace",))
    return {"workspace": parse_workspace(params.get("workspace", "all"), allow_all=True)}


def _sessions_get(params: dict) -> dict:
    check_keys(params, ("workspace", "chat"))
    for key in ("workspace", "chat"):
        if key not in params:
            raise _bad(f"{key} is required")
    return {
        "workspace": parse_workspace(params["workspace"], allow_all=False),
        "chat": parse_chat(params["chat"]),
    }


def _actions_request(params: dict) -> dict:
    check_keys(params, ("workspace", "chat", "action", "expected_turn", "reason"))
    for key in ("workspace", "action"):
        if key not in params:
            raise _bad(f"{key} is required")
    action = params["action"]
    if not isinstance(action, str) or action not in ACTIONS:
        raise _bad("action must be one of " + ", ".join(ACTIONS))
    out = {
        "workspace": parse_workspace(params["workspace"], allow_all=False),
        "action": action,
        "chat": None,
        "expected_turn": None,
        "reason": parse_reason(params.get("reason", "")),
    }
    if action in SERVICE_ACTIONS:
        if "chat" in params or "expected_turn" in params:
            raise _bad(
                f"{action} addresses a whole account; chat and expected_turn are not accepted"
            )
        return out
    if "chat" not in params:
        raise _bad(f"{action} requires chat")
    out["chat"] = parse_chat(params["chat"])
    if "expected_turn" in params:
        if action != "continue":
            raise _bad("expected_turn applies only to continue")
        out["expected_turn"] = parse_turn(params["expected_turn"])
    return out


def _requests_get(params: dict) -> dict:
    check_keys(params, ("request_id",))
    if "request_id" not in params:
        raise _bad("request_id is required")
    return {"request_id": parse_request_id(params["request_id"])}


def _connectors_request(params: dict) -> dict:
    check_keys(params, ("connector", "target"))
    if params.get("connector") != CONNECTOR or params.get("target") != CONNECTOR_TARGET:
        raise _bad("only the reviewed connector 'guardian' with target 'chatgpt' can be requested")
    return {"connector": CONNECTOR, "target": CONNECTOR_TARGET}


_VALIDATORS = {
    "accounts.list": _no_params,
    "sessions.list": _sessions_list,
    "sessions.get": _sessions_get,
    "actions.request": _actions_request,
    "requests.list": _no_params,
    "requests.get": _requests_get,
    "connectors.list": _no_params,
    "connectors.request": _connectors_request,
}


def validate_params(method: object, params: object) -> dict:
    """Return normalized params for ``method`` or raise a fail-closed ``ApiError``."""
    validator = _VALIDATORS.get(method) if isinstance(method, str) else None
    if validator is None:
        raise ApiError("unknown_method", f"unknown method {_shown(method)}")
    if not isinstance(params, dict):
        raise _bad("params must be a JSON object")
    return validator(params)


def parse_envelope(obj: object) -> tuple[str, dict]:
    """Split a ``{method, params?}`` envelope; extra keys fail closed."""
    if not isinstance(obj, dict):
        raise ApiError("invalid_request", "request must be a JSON object")
    check_keys(obj, ("method", "params"))
    method = obj.get("method")
    if not isinstance(method, str):
        raise ApiError("invalid_request", "method must be a string")
    params = obj.get("params", {})
    if not isinstance(params, dict):
        raise ApiError("invalid_request", "params must be a JSON object")
    return method, params
