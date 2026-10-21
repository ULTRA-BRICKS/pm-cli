"""Services (the injected collaborators) and the request-submission path shared by every front end.

Accessed via: ``api.dispatch`` (CLI, HTTP, MCP) and ``poller`` (proposals).

Invariants: submitting a request only validates it against FRESH evidence, then appends one
``request_created`` event; nothing here asks the owner, contacts Guardian or changes any control.
The read-decide-append runs in one ledger transaction, so two concurrent submitters of the same
(account, chat, turn, action, plan digest) get the SAME receipt and one ledger line. For
``continue`` a duplicate always returns the original receipt whatever its status (a rejected or
expired turn is never asked again); the single exception is ``delivery_unavailable``, where the
owner provably saw no card, so an explicit new request is allowed. Turn-less actions deduplicate
only while the original is still active.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..store import resolve_store_dir
from . import policy
from .errors import ApiError
from .ledger import Ledger
from .projection import ACTIVE, Projection, RequestState, project
from .schema import clean_text

MAX_ACTIVE = 32
LIST_LIMIT = 200
TITLE_LIMIT = 80


def default_state_dir() -> Path:
    return resolve_store_dir() / "chatgpt"


def default_fetch(accounts: tuple[str, ...]) -> dict:
    from .. import chatgpt_monitor

    return chatgpt_monitor.fetch_combined(tuple(accounts))


def default_accounts() -> dict:
    from .. import chatgpt_monitor

    return chatgpt_monitor.accounts_report()


@dataclass
class Services:
    state_dir: Path = field(default_factory=default_state_dir)
    clock: Callable[[], float] = time.time
    fetch: Callable[[tuple[str, ...]], dict] = default_fetch
    accounts: Callable[[], dict] = default_accounts

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.state_dir)

    def snapshot(self) -> Projection:
        return project(self.ledger.read_all(), self.clock())


def _refusal(decision: policy.Decision) -> ApiError:
    text = decision.reason + (f" ({decision.detail})" if decision.detail else "")
    if decision.reason == "account-unavailable":
        return ApiError("account_unavailable", f"telemetry for this account is unavailable: {text}")
    if decision.reason == "session-missing":
        return ApiError("not_found", "that chat is not in this account's current session list")
    return ApiError("refused", text)


def _evidence_for_chat_action(services: Services, params: dict) -> str:
    """Validate a watch/unwatch/pause/resume/service request with the existing typed planner."""
    from ..chatgpt_tui import plan_action

    account, action, chat = params["workspace"], params["action"], params["chat"]
    data: dict = {"workspace": account, "accounts": [], "sessions": []}
    title = ""
    if chat is not None or action == "service-start":
        data = services.fetch((account,))
        entries = [a for a in data.get("accounts", []) if a.get("workspace") == account]
        if not entries or entries[0].get("ok") is not True:
            raise ApiError(
                "account_unavailable",
                f"telemetry for {account} is unavailable: "
                + clean_text((entries[0].get("error") if entries else "") or "no data", 200),
            )
    now = services.clock()
    planned, refusal = plan_action(data, {"account": account, "chat": chat}, action, now)
    if planned is None:
        code = "not_found" if refusal.startswith("chat is not in") else "refused"
        raise ApiError(code, clean_text(refusal, 200))
    if chat is not None:
        rows = [
            r for r in data["sessions"] if r.get("account") == account and r.get("chat") == chat
        ]
        title = clean_text(rows[0].get("title", ""), TITLE_LIMIT) if rows else ""
    return title


def _evidence_for_continue(services: Services, params: dict) -> tuple[str, str]:
    account, chat = params["workspace"], params["chat"]
    now = services.clock()
    data = services.fetch((account,))
    _, row, refusal = policy.locate(data, account, chat, now)
    if refusal is not None:
        raise _refusal(refusal)
    turn = params["expected_turn"] or row.get("turn")
    if not isinstance(turn, str) or not turn:
        raise ApiError("refused", "missing-turn (no exact latest turn is observed)")
    verdict = policy.evaluate_continue(data, account, chat, turn, now, require_quiet=False)
    if not verdict.ok:
        raise _refusal(verdict)
    return clean_text(row.get("title", ""), TITLE_LIMIT), turn


def _find_duplicate(
    proj: Projection, key: str, action: str, *, reusable: Callable[[RequestState], bool] | None
) -> RequestState | None:
    matches = [s for s in proj.requests.values() if s.dedup_key == key]
    if not matches:
        return None
    newest = matches[-1]  # ledger order, not wall time, decides which request is newest
    if action == policy.CONTINUE:
        return None if newest.status == "delivery_unavailable" else newest
    if newest.status in ACTIVE or (reusable is not None and reusable(newest)):
        return newest
    return None


def _create(
    services: Services,
    *,
    source: str,
    account: str | None,
    chat: str | None,
    chat_title: str,
    expected_turn: str | None,
    action: str,
    digest: str,
    reason: str,
    plan: dict,
    reusable: Callable[[RequestState], bool] | None = None,
) -> dict:
    now = services.clock()
    key = policy.dedup_key(account, chat, expected_turn, action, digest)
    with services.ledger.transaction() as tx:
        proj = project(tx.events(), now)
        duplicate = _find_duplicate(proj, key, action, reusable=reusable)
        if duplicate is not None:
            return {**duplicate.receipt(), "duplicate": True}
        if sum(s.status in ACTIVE for s in proj.requests.values()) >= MAX_ACTIVE:
            raise ApiError("busy", "too many unresolved approval requests; retry after they settle")
        request_id = str(uuid.uuid4())
        expires = policy.expires_at(now)
        tx.append(
            {
                "kind": "request_created",
                "at": now,
                "request_id": request_id,
                "dedup_key": key,
                "source": source,
                "account": account,
                "chat": chat,
                "chat_title": chat_title,
                "expected_turn": expected_turn,
                "action": action,
                "plan_digest": digest,
                "plan": plan,
                "reason": reason,
                "expires_at": expires,
            }
        )
        state = project(tx.events(), now).requests[request_id]
    return {**state.receipt(), "duplicate": False}


def _prior_receipt(
    services: Services,
    key: str,
    action: str,
    reusable: Callable[[RequestState], bool] | None = None,
) -> dict | None:
    """The original receipt for ``key`` when it is a duplicate; read-only, before any evidence."""
    duplicate = _find_duplicate(services.snapshot(), key, action, reusable=reusable)
    return None if duplicate is None else {**duplicate.receipt(), "duplicate": True}


def submit_action(services: Services, params: dict, source: str = "api") -> dict:
    """Queue one owner-approval request for a validated ``actions.request`` params object."""
    action, account, chat = params["action"], params["workspace"], params["chat"]
    turn = params["expected_turn"]
    if action != policy.CONTINUE or turn is not None:
        digest = policy.plan_digest(account, chat, turn, action)
        prior = _prior_receipt(
            services, policy.dedup_key(account, chat, turn, action, digest), action
        )
        if prior is not None:
            return prior
    if action == policy.CONTINUE:
        title, turn = _evidence_for_continue(services, params)
    else:
        title = _evidence_for_chat_action(services, params)
    digest = policy.plan_digest(account, chat, turn, action)
    return _create(
        services,
        source=source,
        account=account,
        chat=chat,
        chat_title=title,
        expected_turn=turn,
        action=action,
        digest=digest,
        reason=params["reason"],
        plan={"effect": policy.EFFECTS[action]},
    )


def submit_connector(services: Services, params: dict, source: str = "api") -> dict:
    from . import connector

    plan = connector.plan()
    digest = policy.plan_digest(
        None, None, None, policy.CONNECTOR_INSTALL, scope=plan["plan_digest"]
    )

    def installed(state: RequestState) -> bool:
        return state.status == "applied" and connector.local_state(services.state_dir)["state"] in (
            "local_ready",
            "awaiting_platform_connection",
        )

    return _create(
        services,
        source=source,
        account=None,
        chat=None,
        chat_title="",
        expected_turn=None,
        action=policy.CONNECTOR_INSTALL,
        digest=digest,
        reason="",
        plan={
            "effect": policy.EFFECTS[policy.CONNECTOR_INSTALL],
            "connector_plan": plan["plan_digest"],
        },
        reusable=installed,
    )


def list_requests(services: Services) -> dict:
    proj = services.snapshot()
    states = list(reversed(proj.requests.values()))  # newest first: ledger order is authoritative
    return {"requests": [s.receipt() for s in states[:LIST_LIMIT]]}


def get_request(services: Services, request_id: str) -> dict:
    state = services.snapshot().requests.get(request_id)
    if state is None:
        raise ApiError("not_found", "no such request")
    return state.detail()
