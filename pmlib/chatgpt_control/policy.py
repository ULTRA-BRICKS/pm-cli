"""Plan digests, dedup keys and the continuation guards. Pure: no I/O, no clock of its own.

Accessed via: ``service`` (request time), ``worker`` (immediately before execution) and
``poller`` (proposal time). One function, ``evaluate_continue``, decides all three so a request
can never pass a weaker check than a proposal.

Invariants: a continuation is allowed (to ASK the owner, and again to execute) only for a fresh,
enrolled, present, uniquely-matched, non-generating latest turn that is exactly the approved
turn, with no stop/quota/uncertain-send/auth/draft/attachment blocker. Freshness is judged per
session (``session_stale``/``check_stale`` and ``last_checked_at`` against its backoff), not by
the snapshot age alone. Missing evidence is a refusal, never a guess. The passive monitor never
verifies model or effort (``effort`` is always empty, ``model`` is only visible trigger text), so
neither is required here and neither is ever guessed: the exact model/effort proof, and the
refusal when it fails, belong to Guardian after the owner approves. A displayed ``Thinking failed`` belongs to Guardian's own recovery path and is refused
here; this module never weakens or imitates it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from ..chatgpt_tui import _is_stale

TTL_SECONDS = 900
DEFAULT_RETRY_SECONDS = 60  # Guardian's monitor defaults; its per-session backoff overrides
OBSERVATION_GRACE_SECONDS = 120
QUIET_SECONDS = 120
PROPOSAL_NOTE = "A complete answer may not mean the task is finished; review the latest reply."
ASK_MIN_SECONDS = 30
PLAN_SCHEMA = 1
CONTINUE = "continue"
CONNECTOR_INSTALL = "connector-install"

# Fixed, human-readable effect of each approved action: part of what the digest binds.
EFFECTS = {
    "watch": "record a Guardian watch for exactly this chat; does not enable recovery sends",
    "unwatch": "record a Guardian unwatch for exactly this chat",
    "pause": "pause Guardian recovery for exactly this chat (stop --chat)",
    "resume": "resume Guardian for exactly this chat; global, quota and uncertain blocks stay",
    "service-start": "start this account's Guardian recovery user service",
    "service-stop": "stop this account's Guardian recovery user service",
    CONTINUE: "send the single fixed continuation message to exactly this turn through Guardian",
    CONNECTOR_INSTALL: "install the fixed local MCP launcher and manifest for the guardian connector",
}


def plan_digest(
    account: str | None,
    chat: str | None,
    expected_turn: str | None,
    action: str,
    scope: str | None = None,
) -> str:
    plan = {
        "v": PLAN_SCHEMA,
        "account": account,
        "chat": chat,
        "turn": expected_turn,
        "action": action,
        "effect": EFFECTS[action],
        "scope": scope,
    }
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def dedup_key(
    account: str | None,
    chat: str | None,
    expected_turn: str | None,
    action: str,
    digest: str,
) -> str:
    parts = [account, chat, expected_turn, action, digest]
    return hashlib.sha256("\x1f".join(p or "" for p in parts).encode()).hexdigest()


def expires_at(now: float) -> int:
    return int(now) + TTL_SECONDS


def is_expired(expiry: int, now: float) -> bool:
    return now >= expiry


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""
    detail: str = ""


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _blocked_input(row: dict) -> str:
    if row.get("auth_challenge") or "challenge" in str(row.get("error_detail") or "").lower():
        return "auth-challenge"
    if any(row.get(k) for k in ("draft", "has_draft", "attachments", "has_attachments")):
        return "draft-or-attachments"
    return ""


def _check_is_stale(row: dict, now: float) -> bool:
    """Per-session freshness: Guardian's own flags first, then the check time against backoff."""
    if row.get("session_stale") or row.get("check_stale"):
        return True
    checked = row.get("last_checked_at")
    if not _number(checked):
        return True
    backoff = row.get("backoff_seconds")
    allowed = (backoff if _number(backoff) else DEFAULT_RETRY_SECONDS) + OBSERVATION_GRACE_SECONDS
    return now - checked > allowed


def _recovery_blocker(row: dict) -> str:
    if "recovery" not in row:
        return ""
    recovery = row["recovery"]
    if not isinstance(recovery, dict):
        return "recovery-blocked"
    if (
        recovery.get("enrolled") is not True
        or recovery.get("blockers")
        or recovery.get("hold")
        or any(recovery.get(k) for k in ("draft", "challenge", "quota_latched", "unresolved_send"))
    ):
        return "recovery-blocked"
    return ""


def _completion_invalidated(row: dict) -> bool:
    """A completion Guardian retracted stays untrusted until a later completion is observed."""
    invalidated = row.get("completion_invalidated_at")
    if invalidated is None:
        return False
    completed = row.get("completion_observed_at")
    return not (_number(invalidated) and _number(completed) and completed > invalidated)


def _account_veto(entry: dict) -> str:
    controls = entry.get("effective_controls")
    if not isinstance(controls, dict):
        return ""
    if controls.get("global_stop"):
        return "global-stop"
    if controls.get("quota_latched"):
        return "quota-latched"
    return ""


def _eligibility(row: dict, now: float) -> str:
    """Reason a row cannot take a continuation, or ``""``; independent of turn and quiet time."""
    if row.get("observation") != "current":
        return "observation-not-current"
    if _check_is_stale(row, now):
        return "session-stale"
    if row.get("page_count", 1) != 1:
        return "ambiguous-pages"
    if row.get("error"):
        return "observation-error"
    if row.get("watched") is not True:
        return "not-enrolled"
    if row.get("present") is not True:
        return "not-present"
    if row.get("reason"):
        return "blocker"
    if _recovery_blocker(row):
        return "recovery-blocked"
    if row.get("last_go_state") in ("uncertain", "reserved"):
        return "send-uncertain"
    blocked = _blocked_input(row)
    if blocked:
        return blocked
    status, running = row.get("status"), row.get("running")
    if status == "Thinking failed":
        return "failed-turn-uses-recovery"
    if status == "quota-or-rate-limit":
        return "quota"
    if status == "working":
        return "running"
    if running is None:
        return "running-unknown"
    if running is True:
        return "contradictory-completed-and-running" if status == "complete" else "running"
    if status not in ("complete", "unknown"):
        return "status-not-eligible"
    if row.get("continuation") == "running":
        return "continuation-running"
    if _completion_invalidated(row):
        return "completion-invalidated"
    return ""


def locate(
    data: dict, account: str, chat: str, now: float
) -> tuple[dict | None, dict | None, Decision | None]:
    """The account entry, the single matching session row, or the refusal explaining why not."""
    entries = [a for a in data.get("accounts", []) if a.get("workspace") == account]
    entry = entries[0] if len(entries) == 1 else None
    if entry is None or entry.get("ok") is not True:
        return (
            None,
            None,
            Decision(False, "account-unavailable", str((entry or {}).get("error") or "")[:200]),
        )
    if _is_stale(entry.get("generated_at"), entry.get("stale", True), now):
        return entry, None, Decision(False, "account-stale")
    rows = [
        r
        for r in data.get("sessions", [])
        if r.get("account") == account
        and isinstance(r.get("chat"), str)
        and r["chat"].rstrip("/") == chat
    ]
    if not rows:
        return entry, None, Decision(False, "session-missing")
    if len(rows) > 1:
        return entry, None, Decision(False, "session-duplicate")
    return entry, rows[0], None


def evaluate_continue(
    data: dict,
    account: str,
    chat: str,
    expected_turn: str,
    now: float,
    *,
    require_quiet: bool,
) -> Decision:
    entry, row, refusal = locate(data, account, chat, now)
    if refusal is not None:
        return refusal
    vetoed = _account_veto(entry)
    if vetoed:
        return Decision(False, vetoed)
    reason = _eligibility(row, now)
    if reason:
        return Decision(False, reason, str(row.get("reason") or "")[:120])
    turn = row.get("turn")
    if not isinstance(turn, str) or not turn:
        return Decision(False, "missing-turn")
    if turn != expected_turn:
        return Decision(False, "turn-changed")
    if require_quiet:
        progress = row.get("last_progress_at")
        if not _number(progress) or now - progress < QUIET_SECONDS:
            return Decision(False, "not-quiet")
    return Decision(True)
