"""Pure, deterministic fold of ledger events into request receipts.

Accessed via: ``service`` (dedup, receipts), ``worker`` (what to process), ``poller`` (what was
already asked), ``api`` (``requests.*``).

Invariants: the only inputs are the ledger events and an explicit ``now``; there is no clock, no
I/O, no mutable side store. Replay is lenient: an event that is not a legal move from the
request's current status is recorded in ``Projection.errors`` and NOT applied, so one bad line
can neither crash the fold nor reopen a terminal request. Statuses that depend on time
(expiry, an ask whose answer never arrived, a dispatch whose outcome was never recorded) are
derived from ``now`` and never invent a success.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ACTIVE = frozenset({"queued", "asked", "approved", "dispatching", "dispatch_queued"})
ASK_GRACE_SECONDS = 30
DISPATCH_GRACE_SECONDS = 120

_ASK_OUTCOMES = {
    "approved": "approved",
    "rejected": "rejected",
    "timed_out": "timed_out",
    "delivery_unavailable": "delivery_unavailable",
    "uncertain": "asked_post_uncertain",
}
_DISPATCH_OUTCOMES = {
    "queued": "dispatch_queued",
    "applied": "applied",
    "refused": "refused",
    "failed": "failed",
    "uncertain": "uncertain",
}
_CONTINUATION_STATES = {
    "queued": "dispatch_queued",
    "sent": "sent",
    "refused": "refused",
    "uncertain": "uncertain",
}
_DETAIL_LIMIT = 300


@dataclass
class RequestState:
    request_id: str
    dedup_key: str
    source: str
    account: str | None
    chat: str | None
    chat_title: str
    expected_turn: str | None
    action: str
    plan_digest: str
    reason: str
    created_at: float
    expires_at: int
    status: str = "queued"
    status_reason: str = ""
    approved_at: float | None = None
    ask_started_at: float | None = None
    ask_timeout: float = 0
    dispatch_started_at: float | None = None
    timeline: list[dict] = field(default_factory=list)

    def receipt(self) -> dict:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "status_reason": self.status_reason,
            "action": self.action,
            "workspace": self.account,
            "chat": self.chat,
            "expected_turn": self.expected_turn,
            "expires_at": self.expires_at,
            "plan_digest": self.plan_digest,
            "source": self.source,
            "created_at": self.created_at,
            "approved_at": self.approved_at,
        }

    def detail(self) -> dict:
        return {**self.receipt(), "chat_title": self.chat_title, "timeline": list(self.timeline)}


@dataclass
class Projection:
    requests: dict[str, RequestState] = field(default_factory=dict)
    observations: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _text(value: object) -> str:
    return value[:_DETAIL_LIMIT] if isinstance(value, str) else ""


def _number(value: object) -> float | None:
    return float(value) if type(value) in (int, float) else None


def _created(event: dict) -> RequestState | None:
    try:
        expires = event["expires_at"]
        created = event["at"]
        account, chat, turn = event.get("account"), event.get("chat"), event.get("expected_turn")
        if (
            type(expires) is not int
            or type(created) not in (int, float)
            or not isinstance(event["request_id"], str)
            or not isinstance(event["dedup_key"], str)
            or not isinstance(event["action"], str)
            or not isinstance(event["plan_digest"], str)
            or any(x is not None and not isinstance(x, str) for x in (account, chat, turn))
        ):
            return None
        return RequestState(
            request_id=event["request_id"],
            dedup_key=event["dedup_key"],
            source=_text(event.get("source")),
            account=account,
            chat=chat,
            chat_title=_text(event.get("chat_title")),
            expected_turn=turn,
            action=event["action"],
            plan_digest=event["plan_digest"],
            reason=_text(event.get("reason")),
            created_at=float(created),
            expires_at=expires,
        )
    except KeyError:
        return None


def _apply(state: RequestState, event: dict) -> str | None:
    """Apply one event to ``state``; return an error text when the move is illegal."""
    kind, at, status = event["kind"], event["at"], state.status
    detail = _text(event.get("detail"))
    if kind == "ask_started":
        if status != "queued":
            return f"ask_started from {status}"
        state.status, state.ask_started_at = "asked", float(at)
        state.ask_timeout = _number(event.get("timeout_s")) or 0
    elif kind == "ask_abandoned":
        if status != "asked":
            return f"ask_abandoned from {status}"
        state.status, state.status_reason = "asked_post_uncertain", detail or "ask interrupted"
    elif kind == "ask_result":
        outcome = _ASK_OUTCOMES.get(event.get("outcome"))
        if status != "asked" or outcome is None:
            return f"ask_result {event.get('outcome')!r} from {status}"
        state.status, state.status_reason = outcome, detail
        if outcome == "approved":
            state.approved_at = float(at)
    elif kind == "request_expired":
        if status not in ("queued", "approved"):
            return f"request_expired from {status}"
        state.status = "expired"
    elif kind == "request_invalidated":
        if status not in ("queued", "approved"):
            return f"request_invalidated from {status}"
        state.status, state.status_reason = "invalidated", _text(event.get("reason"))
    elif kind == "dispatch_started":
        if status != "approved":
            return f"dispatch_started from {status}"
        state.status, state.dispatch_started_at = "dispatching", float(at)
    elif kind == "dispatch_result":
        outcome = _DISPATCH_OUTCOMES.get(event.get("outcome"))
        if status != "dispatching" or outcome is None:
            return f"dispatch_result {event.get('outcome')!r} from {status}"
        state.status, state.status_reason = outcome, detail
    elif kind == "continuation_status":
        outcome = _CONTINUATION_STATES.get(event.get("state"))
        if (
            state.action != "continue"
            or status not in ("dispatching", "dispatch_queued", "uncertain")
            or outcome is None
        ):
            return f"continuation_status {event.get('state')!r} from {status}"
        state.status, state.status_reason = outcome, detail
    else:
        return f"unknown event kind {kind!r}"
    state.timeline.append({"at": at, "event": kind, **({"detail": detail} if detail else {})})
    return None


def _finalize(state: RequestState, now: float) -> None:
    if state.status in ("queued", "approved") and now >= state.expires_at:
        state.status, state.status_reason = "expired", "approval window elapsed"
    elif (
        state.status == "asked"
        and state.ask_started_at is not None
        and now > state.ask_started_at + state.ask_timeout + ASK_GRACE_SECONDS
    ):
        state.status, state.status_reason = "asked_post_uncertain", "no owner answer was recorded"
    elif (
        state.status == "dispatching"
        and state.dispatch_started_at is not None
        and now > state.dispatch_started_at + DISPATCH_GRACE_SECONDS
    ):
        state.status, state.status_reason = "uncertain", "dispatch outcome was not recorded"


def project(events: list[dict], now: float) -> Projection:
    proj = Projection()
    for event in events:
        kind = event.get("kind")
        if kind == "observation":
            proj.observations.append(event)
            continue
        request_id = event.get("request_id")
        if kind == "request_created":
            state = _created(event)
            if state is None or state.request_id in proj.requests:
                proj.errors.append("request_created malformed or duplicated")
                continue
            state.timeline.append({"at": event["at"], "event": kind})
            proj.requests[state.request_id] = state
            continue
        state = proj.requests.get(request_id) if isinstance(request_id, str) else None
        if state is None:
            proj.errors.append(f"{kind} for unknown request")
            continue
        if type(event.get("at")) not in (int, float):
            proj.errors.append(f"{kind} without time")
            continue
        error = _apply(state, event)
        if error:
            proj.errors.append(f"{state.request_id}: {error}")
    for state in proj.requests.values():
        _finalize(state, now)
    return proj
