"""Conservative continuation proposals from fresh telemetry; never an automatic continuation.

Accessed via: ``runner`` (``pm chatgpt approvals run``), once per interval.

A completed answer is not a completed task, so the poller only PROPOSES: it queues one
owner-approval request per account/chat/turn when the chat is enrolled, present, observed
currently, non-generating (``complete`` or ``unknown``), blocker-free, and has been quiet for
at least ``QUIET_SECONDS`` (Guardian's ``last_progress_at``, which
survives pm restarts). The very same ``policy.evaluate_continue`` that guards execution decides.

Invariants: nothing is proposed for active generation, missing/stale/ambiguous evidence, a
contradictory completed-and-running row, an unresolved send, quota or stop, or a failed turn
(Guardian's own recovery path owns that and stays independent). A turn that already has ANY
continue request (answered, rejected, expired, unanswered) is never proposed again; only a
``delivery_unavailable`` request is retried, after a cooldown, because the owner saw no card.
A running chat with no visible progress is REPORTED once and never continued. State survives a
restart because every fact is read from the ledger.
"""

from __future__ import annotations

import hashlib

from ..chatgpt_monitor import ACCOUNTS
from . import policy, service
from .errors import ApiError
from .projection import ACTIVE, Projection
from .schema import clean_text
from .service import Services

MAX_PROPOSALS_PER_TICK = 2
ACTIVE_PROPOSAL_CAP = 4
DELIVERY_COOLDOWN_SECONDS = 900


def _last_event_at(state) -> float:
    return state.timeline[-1]["at"] if state.timeline else state.created_at


def _already_handled(proj: Projection, account: str, chat: str, turn: str, now: float) -> bool:
    matches = [
        s
        for s in proj.requests.values()
        if s.action == policy.CONTINUE
        and (s.account, s.chat, s.expected_turn) == (account, chat, turn)
    ]
    if not matches:
        return False
    newest = matches[-1]
    if newest.status == "delivery_unavailable":
        return now - _last_event_at(newest) < DELIVERY_COOLDOWN_SECONDS
    return True


def _chat_busy(proj: Projection, account: str, chat: str) -> bool:
    return any(
        s.status in ACTIVE and (s.account, s.chat) == (account, chat)
        for s in proj.requests.values()
    )


def _stalled(row: dict) -> bool:
    return (
        row.get("observation") == "current"
        and row.get("watched") is True
        and (row.get("status") == "working" or row.get("running") is True)
        and row.get("no_visible_progress") is True
    )


def _report_stalled(services: Services, proj: Projection, account: str, row: dict, now: float):
    turn = row.get("turn") if isinstance(row.get("turn"), str) else ""
    key = hashlib.sha256(
        "\x1f".join([account, row["chat"], turn, str(row.get("last_progress_at"))]).encode()
    ).hexdigest()
    if any(o.get("key") == key for o in proj.observations):
        return None
    services.ledger.append(
        {
            "kind": "observation",
            "at": now,
            "key": key,
            "obs": "no_visible_progress",
            "account": account,
            "chat": row["chat"],
            "turn": turn,
        }
    )
    return {
        "observation": "no_visible_progress",
        "workspace": account,
        "chat": row["chat"],
        "title": clean_text(row.get("title", ""), 60),
        "note": "generation shows no visible progress; reported only, never continued",
    }


def poll_once(services: Services) -> dict:
    now = services.clock()
    report: dict = {
        "proposed": [],
        "reported": [],
        "skipped": {},
        "accounts": {},
        "error": None,
    }

    def skip(reason: str) -> None:
        report["skipped"][reason] = report["skipped"].get(reason, 0) + 1

    try:
        data = services.fetch(ACCOUNTS)
    except Exception:  # noqa: BLE001 - an unreadable snapshot is reported, never fatal
        report["error"] = "telemetry unavailable"
        return report
    proj = services.snapshot()
    for entry in data["accounts"]:
        account = entry.get("workspace")
        report["accounts"][account] = entry.get("ok") is True
        if entry.get("ok") is not True:
            skip("account-unavailable")
    for row in data["sessions"]:
        account, chat, turn = row.get("account"), row.get("chat"), row.get("turn")
        if account not in ACCOUNTS or not isinstance(chat, str):
            skip("malformed-row")
            continue
        chat = chat.rstrip("/")
        if _stalled(row) and (stalled := _report_stalled(services, proj, account, row, now)):
            report["reported"].append(stalled)
            proj = services.snapshot()
        decision = policy.evaluate_continue(
            data, account, chat, turn if isinstance(turn, str) else "", now, require_quiet=True
        )
        if not decision.ok:
            skip(decision.reason)
            continue
        if _already_handled(proj, account, chat, turn, now):
            skip("already-handled")
            continue
        if _chat_busy(proj, account, chat):
            skip("chat-busy")
            continue
        open_proposals = sum(
            s.status in ACTIVE and s.source == "poller" for s in proj.requests.values()
        )
        if (
            len(report["proposed"]) >= MAX_PROPOSALS_PER_TICK
            or open_proposals >= ACTIVE_PROPOSAL_CAP
        ):
            skip("proposal-cap")
            continue
        try:
            receipt = service.submit_action(
                services,
                {
                    "workspace": account,
                    "action": policy.CONTINUE,
                    "chat": chat,
                    "expected_turn": turn,
                    "reason": policy.PROPOSAL_NOTE,
                },
                source="poller",
            )
        except ApiError as exc:
            skip(f"request-refused:{exc.code}")
            continue
        proj = services.snapshot()
        if not receipt["duplicate"]:
            report["proposed"].append(
                {
                    "request_id": receipt["request_id"],
                    "workspace": account,
                    "chat": chat,
                    "turn": turn,
                }
            )
    return report
