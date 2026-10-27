"""Conservative continuation proposals from fresh telemetry; never an automatic continuation.

Accessed via: ``runner`` (``pm chatgpt approvals run``), once per interval.

A completed answer is not a completed task, so the poller only PROPOSES: it queues one
owner-approval request per account/chat/turn when the chat is enrolled, present, observed
currently, non-generating (``complete`` or ``unknown``), blocker-free, with a known model and
effort, and has been quiet for at least ``QUIET_SECONDS`` (Guardian's ``last_progress_at``, which
survives pm restarts). The very same ``policy.evaluate_continue`` that guards execution decides.

Invariants: nothing is proposed for active generation, missing/stale/ambiguous evidence, a
contradictory completed-and-running row, an unresolved send, quota or stop, or a failed turn
(Guardian's own recovery path owns that and stays independent). A turn that already has ANY
continue request (answered, rejected, expired, unanswered) is never proposed again; only a
``delivery_unavailable`` request is retried, after a cooldown, because the owner saw no card.
A running chat with no visible progress is REPORTED once and never continued. State survives a
restart because every fact is read from the ledger.

The audit cache is consulted read-only (``audit_store``): a trusted ``done``/``working``/``blocked``
verdict suppresses the owner question; anything else, or any doubt, proposes as before. The poller
cannot read the chat, so the cached fingerprint is NOT compared; instead Guardian telemetry must
show no progress after the verdict was confirmed, a non-running row and a current observation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from ..chatgpt_monitor import ACCOUNTS
from . import audit_classifier, audit_decision, audit_store, audit_urls, policy, service
from .errors import ApiError
from .projection import ACTIVE, Projection
from .schema import clean_text
from .service import Services

MAX_PROPOSALS_PER_TICK = 2
ACTIVE_PROPOSAL_CAP = 4
DELIVERY_COOLDOWN_SECONDS = 900
# The timer period is 30 min, so a verdict not re-confirmed within an hour is stale.
AUDIT_VERDICT_MAX_AGE = 3600.0
_AUDIT_SKIPS = {"done": "task-finished", "working": "task-working", "blocked": "task-blocked"}


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


def default_expected_model() -> str:
    """The model identities a cached verdict must come from; empty when the provider is down."""
    return audit_classifier.LayaClassifier().profile()


def _identity_proven(entry, row: dict) -> bool:
    """A cached verdict may suppress only for the turn it was made for, on proven route meta.

    Progress metadata can lag behind a new user request, so timing alone proves nothing: the row's
    rendered turn must be one of the native identifiers the verdict recorded. A ``done`` verdict
    additionally needs an ``ok`` classification whose validated, non-truncated route meta names
    the same checkpoint and window as the model the entry was bound to.
    """
    turn = row.get("turn")
    if not isinstance(turn, str) or turn not in entry.turn_ids:
        return False
    if entry.task_state != "done":
        return True
    verdict = entry.verdict
    return verdict.get("classifier_status") == "ok" and audit_classifier.route_meta_matches(
        verdict.get("classifier_meta"), entry.model
    )


def _audit_skip_reason(
    services: Services, account: str, chat: str, row: dict, now: float, model: Callable[[], str]
) -> str | None:
    """Why a TRUSTED cached audit verdict says the owner must not be asked, else ``None``.

    Every failure (unparseable URL, unreadable store, model lookup error) yields ``None``: the
    verdict can only add caution, so doubt falls back to today's behaviour of a proposal.
    """
    try:
        conversation_id = audit_urls.parse_chat_url(chat).conversation_id
        store = audit_store.AuditStore(services.audit_dir)
        if store.get(account, conversation_id) is None:
            return None
        entry = store.trusted(
            account,
            conversation_id,
            now=now,
            prompt_version=audit_classifier.PROMPT_VERSION,
            model=model(),
            thresholds_digest=audit_decision.thresholds_digest(audit_decision.DEFAULT_THRESHOLDS),
            max_age=AUDIT_VERDICT_MAX_AGE,
            fingerprint=None,
        )
    except Exception:  # noqa: BLE001 - a broken cache must never change what the poller does
        return None
    if entry is None or row.get("running") is not False or row.get("observation") != "current":
        return None
    if not _identity_proven(entry, row):
        return None
    progress = row.get("last_progress_at")
    if type(progress) in (int, float) and progress > entry.verified_at:
        return None
    return _AUDIT_SKIPS.get(entry.task_state)


def _once(probe: Callable[[], str]) -> Callable[[], str]:
    """Memoise one tick's provider probe (value or failure) so N rows cost one socket round-trip."""
    outcome: list = []

    def cached() -> str:
        if not outcome:
            try:
                outcome.append((True, probe()))
            except Exception as exc:  # noqa: BLE001 - replayed to every caller of this tick
                outcome.append((False, exc))
        ok, value = outcome[0]
        if not ok:
            raise value
        return value

    return cached


def poll_once(services: Services, expected_model: Callable[[], str] | None = None) -> dict:
    model = _once(expected_model or default_expected_model)
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
        if audit_reason := _audit_skip_reason(services, account, chat, row, now, model):
            skip(audit_reason)
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
