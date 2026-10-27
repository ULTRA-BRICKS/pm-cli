"""The approval worker: ask the owner, re-check the world, then execute exactly once.

Accessed via: ``runner`` (``pm chatgpt approvals run``). Never reachable from the API, HTTP or MCP.

One request at a time, in ledger order, each finite (an owner wait is bounded by the request's own
expiry). The lifecycle of a ``continue`` request:

    queued -> [fresh check] -> ask_started (persisted BEFORE the card is posted) -> owner verdict
    -> approved -> [fresh check again, exact turn] -> dispatch_started (persisted BEFORE the call)
    -> Guardian POST /continue -> dispatch_queued -> GET /continuations -> sent | refused | uncertain

Invariants: only ``OwnerVerdict.outcome == 'approved'`` (a verified ``tg-ctl ask`` exit 0) opens the
path to execution. Any ambiguity (an interrupted wait, an unrecognized reply, a crash after the
post or after dispatch) becomes a durable ``asked_post_uncertain`` / ``uncertain`` receipt and is
NEVER retried, re-asked or re-sent; reconciliation only READS Guardian's status. Approval
authorizes exactly the bound account/chat/turn/action: a changed turn, running or blocked chat,
stale or missing evidence, or an expired window invalidates the request
instead of executing it.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

from ..chatgpt_tui import Action, revalidate
from . import connector, policy
from .guardian_client import GuardianConfig, GuardianUnavailable, GuardianUncertain
from .projection import RequestState, project
from .schema import clean_text
from .service import Services
from .tg_transport import OwnerVerdict, build_question

_ASK_OUTCOMES = frozenset(
    {"approved", "rejected", "timed_out", "delivery_unavailable", "uncertain"}
)
_CONTROL_OUTCOMES = {
    "success": "applied",
    "pending": "uncertain",
    "uncertain": "uncertain",
    "failed": "failed",
}
_CONTINUATION_TO_STATUS = {
    "queued": "dispatch_queued",
    "sent": "sent",
    "refused": "refused",
    "uncertain": "uncertain",
}
_TERMINAL_CONTINUATION = ("sent", "refused", "uncertain")
RECONCILE_WINDOW_SECONDS = 86400
DETAIL = 300


class Worker:
    def __init__(
        self,
        services: Services,
        *,
        transport: Callable[[str, int], OwnerVerdict],
        guardian,
        run_control: Callable[[str, str, str | None], object],
        install: Callable,
        owner_ref: Callable[[], str | None],
        poll_seconds: int = 10,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.services = services
        self._transport = transport
        self._guardian = guardian
        self._run_control = run_control
        self._install = install
        self._owner_ref = owner_ref
        self._poll_seconds = poll_seconds
        self._sleep = sleep

    # ledger helpers -------------------------------------------------------------------------
    def _append(self, kind: str, request_id: str, **fields) -> None:
        self.services.ledger.append(
            {"kind": kind, "at": self.services.clock(), "request_id": request_id, **fields}
        )

    def _state(self, request_id: str) -> RequestState:
        return self.services.snapshot().requests[request_id]

    def _report(self, request_id: str) -> dict:
        state = self._state(request_id)
        return {
            "request_id": request_id,
            "action": state.action,
            "workspace": state.account,
            "status": state.status,
            "reason": state.status_reason,
        }

    # recovery -------------------------------------------------------------------------------
    def recover(self) -> None:
        """Resolve what a previous worker left open. The caller holds the worker lock, so any
        ``asked`` or ``dispatching`` request is an orphan: record it as uncertain, never replay."""
        now = self.services.clock()
        with self.services.ledger.transaction() as tx:
            for state in project(tx.events(), now).requests.values():
                if state.status == "asked":
                    tx.append(
                        {
                            "kind": "ask_abandoned",
                            "at": now,
                            "request_id": state.request_id,
                            "detail": "worker restarted before an owner answer was recorded",
                        }
                    )
                elif state.status == "dispatching":
                    tx.append(
                        {
                            "kind": "dispatch_result",
                            "at": now,
                            "request_id": state.request_id,
                            "outcome": "uncertain",
                            "detail": "worker restarted; the dispatch outcome was not recorded",
                        }
                    )

    # evidence -------------------------------------------------------------------------------
    def _fetch(self, account: str) -> dict | None:
        try:
            return self.services.fetch((account,))
        except Exception:  # noqa: BLE001 - unreadable telemetry is simply "unavailable"
            return None

    def _check(self, state: RequestState) -> policy.Decision:
        """The fresh-evidence gate, run before asking and again before executing."""
        now = self.services.clock()
        if state.action == policy.CONNECTOR_INSTALL:
            expected = policy.plan_digest(
                None, None, None, state.action, scope=connector.plan()["plan_digest"]
            )
            return policy.Decision(expected == state.plan_digest, "plan-digest-mismatch")
        expected = policy.plan_digest(state.account, state.chat, state.expected_turn, state.action)
        if expected != state.plan_digest:
            return policy.Decision(False, "plan-digest-mismatch")
        if state.action == "service-stop":
            return policy.Decision(True)
        if state.action == policy.CONTINUE:
            try:
                GuardianConfig().check_state_override(state.account)
            except GuardianUnavailable:
                return policy.Decision(False, "state-scope-mismatch")
        data = self._fetch(state.account)
        entries = [
            a for a in (data or {}).get("accounts", []) if a.get("workspace") == state.account
        ]
        if data is None or not entries or entries[0].get("ok") is not True:
            return policy.Decision(False, "account-unavailable")
        if state.action == policy.CONTINUE:
            return policy.evaluate_continue(
                data, state.account, state.chat, state.expected_turn, now, require_quiet=False
            )
        planned, refusal = revalidate(data, Action(state.account, state.action, state.chat), now)
        if planned is None:
            return policy.Decision(False, clean_text(refusal, 120))
        return policy.Decision(True)

    def _invalidate(self, state: RequestState, decision: policy.Decision, stage: str) -> None:
        self._append(
            "request_invalidated",
            state.request_id,
            reason=decision.reason or "refused",
            stage=stage,
        )

    # asking ---------------------------------------------------------------------------------
    def _ask(self, state: RequestState) -> bool:
        decision = self._check(state)
        if not decision.ok:
            self._invalidate(state, decision, "pre-ask")
            return False
        timeout = min(900, int(state.expires_at - self.services.clock()))
        if timeout < policy.ASK_MIN_SECONDS:
            self._append("request_expired", state.request_id)
            return False
        question = build_question(
            request_id=state.request_id,
            account=state.account,
            chat=state.chat,
            chat_title=state.chat_title,
            turn=state.expected_turn,
            action=state.action,
            plan_digest=state.plan_digest,
            expires_at=state.expires_at,
            reason=state.reason,
            scope_digest=(
                connector.plan()["plan_digest"]
                if state.action == policy.CONNECTOR_INSTALL
                else None
            ),
        )
        owner = self._owner_ref()
        self._append(
            "ask_started",
            state.request_id,
            timeout_s=timeout,
            question_digest=hashlib.sha256(question.encode()).hexdigest(),
            owner_ref=owner,
        )
        if owner is None:
            self._append(
                "ask_result",
                state.request_id,
                outcome="delivery_unavailable",
                detail="no Telegram owner configuration was found; nothing was posted",
            )
            return False
        try:
            verdict = self._transport(question, timeout)
            if not isinstance(verdict, OwnerVerdict) or verdict.outcome not in _ASK_OUTCOMES:
                verdict = OwnerVerdict("uncertain", None, "unrecognized transport result")
        except Exception:  # noqa: BLE001 - the card may or may not exist: uncertain, not failed
            verdict = OwnerVerdict("uncertain", None, "transport raised; card state unknown")
        except BaseException:
            self._append(
                "ask_result",
                state.request_id,
                outcome="uncertain",
                exit_code=None,
                detail="interrupted while waiting for the owner; card state unknown",
            )
            raise
        self._append(
            "ask_result",
            state.request_id,
            outcome=verdict.outcome,
            exit_code=verdict.exit_code,
            detail=clean_text(verdict.detail, DETAIL),
        )
        return verdict.outcome == "approved"

    # executing ------------------------------------------------------------------------------
    def _execute(self, state: RequestState, approved_at: int, expires_at: int) -> tuple[str, str]:
        if state.action == policy.CONTINUE:
            body = {
                "request_id": state.request_id,
                "chat": state.chat,
                "expected_turn": state.expected_turn,
                "approved_at": approved_at,
                "expires_at": expires_at,
            }
            try:
                reply = self._guardian.request_continue(state.account, body)
            except GuardianUnavailable as exc:
                return "failed", str(exc)
            except ValueError as exc:
                return "failed", clean_text(f"continuation request was malformed: {exc}", DETAIL)
            except Exception as exc:  # noqa: BLE001 - GuardianUncertain and anything unforeseen
                detail = str(exc) if isinstance(exc, GuardianUncertain) else "dispatch raised"
                return "uncertain", clean_text(detail, DETAIL)
            return reply.state, clean_text(reply.detail, DETAIL)
        if state.action == policy.CONNECTOR_INSTALL:
            try:
                result = self._install(self.services.state_dir)
            except connector.ConnectorError as exc:
                return "failed", clean_text(str(exc), DETAIL)
            return "applied", "installed" if result.changed else "already installed"
        result = self._run_control(state.account, state.action, state.chat)
        outcome = _CONTROL_OUTCOMES.get(getattr(result, "outcome", ""), "uncertain")
        return outcome, clean_text(getattr(result, "message", ""), DETAIL)

    def _dispatch(self, state: RequestState) -> None:
        if policy.is_expired(state.expires_at, self.services.clock()):
            self._append("request_expired", state.request_id)
            return
        decision = self._check(state)
        if not decision.ok:
            self._invalidate(state, decision, "pre-dispatch")
            return
        approved_at = int(state.approved_at)
        expires_at = min(state.expires_at, approved_at + policy.TTL_SECONDS)
        self._append(
            "dispatch_started", state.request_id, approved_at=approved_at, expires_at=expires_at
        )
        try:
            outcome, detail = self._execute(state, approved_at, expires_at)
        except Exception:  # noqa: BLE001 - something may already have been applied
            outcome, detail = "uncertain", "execution raised unexpectedly; effect unknown"
        except BaseException:
            self._append(
                "dispatch_result",
                state.request_id,
                outcome="uncertain",
                detail="interrupted during execution; effect unknown",
            )
            raise
        self._append("dispatch_result", state.request_id, outcome=outcome, detail=detail)
        if state.action == policy.CONTINUE and outcome == "queued":
            self._poll(state)

    # guardian status ------------------------------------------------------------------------
    def _record_continuation(self, state: RequestState, status) -> bool:
        """Append a status event when Guardian reports something new; True once it is final."""
        if status.state is None:
            return False
        current = self._state(state.request_id).status
        if _CONTINUATION_TO_STATUS[status.state] != current:
            self._append(
                "continuation_status", state.request_id, state=status.state, detail=status.detail
            )
        return status.state in _TERMINAL_CONTINUATION

    def _poll(self, state: RequestState) -> None:
        attempts = max(1, self._poll_seconds)
        for attempt in range(attempts):
            try:
                status = self._guardian.continuation(state.account, state.request_id)
            except Exception:  # noqa: BLE001 - read-only query; leave the receipt as it is
                return
            if self._record_continuation(state, status):
                return
            if attempt < attempts - 1:
                self._sleep(1)

    def reconcile(self) -> None:
        """Read Guardian's status for dispatched continuations; never resends anything."""
        now = self.services.clock()
        for state in list(self.services.snapshot().requests.values()):
            if (
                state.action != policy.CONTINUE
                or state.status not in ("dispatch_queued", "uncertain")
                or now - state.created_at > RECONCILE_WINDOW_SECONDS
            ):
                continue
            try:
                status = self._guardian.continuation(state.account, state.request_id)
            except Exception:  # noqa: BLE001, S112 - Guardian down: keep the existing receipt
                continue
            self._record_continuation(state, status)

    # entry points ---------------------------------------------------------------------------
    def process_next(self) -> dict | None:
        """Handle the oldest open request; ``None`` when nothing is waiting."""
        for state in self.services.snapshot().requests.values():
            if state.status in ("queued", "approved"):
                break
        else:
            return None
        if state.status == "queued" and not self._ask(state):
            return self._report(state.request_id)
        current = self._state(state.request_id)
        if current.status == "approved":
            self._dispatch(current)
        return self._report(state.request_id)

    def tick(self, max_requests: int = 4) -> list[dict]:
        self.reconcile()
        reports = []
        for _ in range(max_requests):
            report = self.process_next()
            if report is None:
                break
            reports.append(report)
        return reports

    def run_once(self, max_requests: int = 4) -> list[dict]:
        self.recover()
        return self.tick(max_requests)
