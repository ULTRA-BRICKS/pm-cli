"""Account-local Guardian client: ``POST /continue`` and ``GET /continuations/<request_id>``.

Accessed via: ``worker`` only, after a verified owner approval and a fresh evidence check.

Contract (design-and-contract.md): the POST body is EXACTLY
``{request_id, chat, expected_turn, approved_at, expires_at}`` with ``expires_at`` at most
``approved_at + 900``; Guardian answers 202 ``{ok:true,request_id,status:'queued'}``
(idempotent); the GET returns queued / refused / sent / uncertain plus a reason.

Invariants: each account has its own loopback port and its own bearer token, found through that
account's Guardian workspace configuration (``~/.config/agent-tools/chatgpt-guardian/<account>.json``
-> state path -> ``control.token`` beside it, which must be a private regular file). The token is
read per call into a local variable, is never an attribute, never appears in an exception, and is
never used for another account. Failures split into ``GuardianUnavailable`` (nothing was sent: no
config, no listener, route absent, credentials rejected) and ``GuardianUncertain`` (the request may
have been accepted); neither is ever retried here.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..chatgpt_monitor import ACCOUNTS
from .schema import TURN, clean_text

DEFAULT_PORTS = {"home": 8790, "glide.vc": 8791}
BODY_KEYS = ("request_id", "chat", "expected_turn", "approved_at", "expires_at")
MAX_REPLY = 65536
MAX_APPROVAL_WINDOW = 900
_STATES = ("queued", "refused", "sent", "uncertain")
_CHAT = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9_-]+")
UNAVAILABLE = "guardian continuation unavailable"


class GuardianUnavailable(Exception):
    """Nothing was sent; the message is safe to show and always starts with the fixed phrase."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"{UNAVAILABLE}: {reason}")


class GuardianUncertain(Exception):
    """The request may or may not have been accepted; never retry automatically."""


@dataclass(frozen=True)
class ContinueReply:
    state: str  # queued | refused
    detail: str


@dataclass(frozen=True)
class ContinuationStatus:
    state: str | None  # None: nothing trustworthy was learned
    detail: str


def _uuid(value: object) -> str:
    try:
        if isinstance(value, str) and str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise ValueError("request_id must be a canonical UUID")


def check_body(body: object) -> dict:
    """Refuse anything but the frozen five-key body before it can reach the network."""
    if not isinstance(body, dict) or set(body) != set(BODY_KEYS):
        raise ValueError("continue body must have exactly " + ", ".join(BODY_KEYS))
    _uuid(body["request_id"])
    chat, turn = body["chat"], body["expected_turn"]
    if not isinstance(chat, str) or _CHAT.fullmatch(chat) is None:
        raise ValueError("chat must be an exact permanent conversation URL")
    if not isinstance(turn, str) or TURN.fullmatch(turn) is None:
        raise ValueError("expected_turn is malformed")
    approved, expires = body["approved_at"], body["expires_at"]
    if type(approved) is not int or type(expires) is not int:
        raise ValueError("approved_at and expires_at must be integer Unix times")
    if not approved <= expires <= approved + MAX_APPROVAL_WINDOW:
        raise ValueError("expires_at must be within 900 seconds after approved_at")
    return dict(body)


class GuardianConfig:
    """Resolves each account's loopback port and bearer token from its own Guardian state path."""

    def __init__(self, home: Path | None = None, ports: dict[str, int] | None = None) -> None:
        self._home = Path(home) if home is not None else Path.home()
        self._ports = dict(DEFAULT_PORTS if ports is None else ports)

    def port(self, account: str) -> int:
        if account not in ACCOUNTS or account not in self._ports:
            raise ValueError("unsupported account")
        return self._ports[account]

    def read_token(self, account: str) -> str:
        """The account's bearer token; raises ``GuardianUnavailable`` without revealing it."""
        self.port(account)
        config = self._home / ".config/agent-tools/chatgpt-guardian" / f"{account}.json"
        try:
            selected = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise GuardianUnavailable(f"{account} Guardian is not configured") from None
        state = selected.get("state") if isinstance(selected, dict) else None
        if (
            not isinstance(selected, dict)
            or set(selected) != {"workspace", "state"}
            or selected["workspace"] != account
            or not isinstance(state, str)
            or not os.path.isabs(state)
        ):
            raise GuardianUnavailable(f"{account} Guardian workspace configuration is invalid")
        token_path = Path(state).parent / "control.token"
        try:
            fd = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise GuardianUnavailable(
                f"{account} control token is missing or not a plain file"
            ) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise GuardianUnavailable(f"{account} control token must be a file you own")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise GuardianUnavailable(f"{account} control token must have mode 0600")
            token = os.read(fd, 4096).decode("utf-8", "replace").strip()
        finally:
            os.close(fd)
        if len(token) < 32 or not token.isprintable() or " " in token:
            raise GuardianUnavailable(f"{account} control token is malformed")
        return token


class GuardianClient:
    def __init__(self, config: GuardianConfig | None = None, timeout: float = 5.0) -> None:
        self._config = config or GuardianConfig()
        self._timeout = timeout

    def _call(self, account: str, method: str, path: str, body: dict | None) -> tuple[int, object]:
        token = self._config.read_token(account)
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(
            "127.0.0.1", self._config.port(account), timeout=self._timeout
        )
        try:
            try:
                conn.connect()
            except OSError:
                raise GuardianUnavailable(f"{account} Guardian is not listening") from None
            try:
                conn.request(method, path, body=payload, headers=headers)
                response = conn.getresponse()
                raw = response.read(MAX_REPLY + 1)
            except (OSError, http.client.HTTPException):
                raise GuardianUncertain(
                    "Guardian did not answer in time; the request may be queued"
                ) from None
        finally:
            conn.close()
        if len(raw) > MAX_REPLY:
            raise GuardianUncertain("Guardian reply was too large to trust")
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        return response.status, data

    def request_continue(self, account: str, body: dict) -> ContinueReply:
        body = check_body(body)
        status, data = self._call(account, "POST", "/continue", body)
        if status in (404, 405, 501):
            raise GuardianUnavailable(f"{account} Guardian has no approved-continuation route")
        if status in (401, 403):
            raise GuardianUnavailable(f"{account} Guardian rejected the control credentials")
        if status == 202:
            if (
                isinstance(data, dict)
                and data.get("ok") is True
                and data.get("request_id") == body["request_id"]
                and data.get("status") == "queued"
            ):
                return ContinueReply("queued", "")
            raise GuardianUncertain("Guardian accepted with an unrecognized reply")
        if 400 <= status < 500:
            detail = data.get("error") if isinstance(data, dict) else None
            return ContinueReply("refused", clean_text(detail or f"HTTP {status}", 200))
        raise GuardianUncertain(f"Guardian answered HTTP {status}")

    def continuation(self, account: str, request_id: str) -> ContinuationStatus:
        _uuid(request_id)
        status, data = self._call(account, "GET", f"/continuations/{request_id}", None)
        if status in (401, 403):
            raise GuardianUnavailable(f"{account} Guardian rejected the control credentials")
        if status != 200 or not isinstance(data, dict):
            return ContinuationStatus(None, f"HTTP {status}")
        reported = data.get("status")
        if data.get("request_id", request_id) != request_id or reported not in _STATES:
            return ContinuationStatus(None, "unrecognized status reply")
        return ContinuationStatus(reported, clean_text(data.get("reason") or "", 200))
