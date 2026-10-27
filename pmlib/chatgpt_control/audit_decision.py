"""Deterministic safety precedence that turns evidence plus Laya scores into three states.

Accessed via: ``audit_collector`` (every conversation), ``audit_bench`` (every fixture) and the
poller (through the cached verdict). Pure: no I/O, no clock of its own, no randomness.

``response_state`` (did the assistant finish a reply), ``task_state`` (is the user's TASK finished:
``done`` / ``incomplete`` / ``blocked`` / ``working`` / ``unknown``) and ``recovery_state`` (what a
caller may do about it) are three SEPARATE fields. The Laya scores are only advisory evidence: a
model score can withhold ``done`` or abstain, but it can never alone make a task ``blocked`` (that
needs positive deterministic evidence of a named permission, login, approval or physical step),
and ``done`` is reachable only when every deterministic gate passes AND the classifier agrees, so
no score, ``end_turn`` flag, tool success or recap makes a task done by itself, and a missing,
failed or malformed classification is ``unknown``, never ``done``.

Precedence (first match wins): wrong/missing account, stale read -> ``unknown``; a pending approval
-> ``blocked``; a live progress indicator or an in-progress assistant message of the CURRENT turn
(after the latest user message) -> ``working`` (an older abandoned in-progress message never
vetoes a newer answered request, neither does an earlier placeholder followed by a finished final
of the same turn, and a current one stalled for longer than
``stale_generation_seconds`` is ``incomplete``, not working forever); truncated context or no
substantive request -> ``unknown``; an expired permission, a login requirement or a human
physical-input need -> ``blocked``; no final answer after the LATEST user message -> ``incomplete``
(an assistant turn that ended with no reply at once, otherwise ``unknown`` inside the
first-response grace); an unfinished or failed final -> ``incomplete``; an artifact-only final
(image or file, no text) -> ``unknown`` (the content cannot be judged). Then, from the final text:
a delivered handoff after an explicit stop/handoff request is ``done`` with no continuation
needed; an announced or admitted unfinished promise is ``incomplete`` BEFORE any model score is
read; a doubt cue (plan-like answer, demanded deployment with no evidence of it) abstains; and only
then the classifier decides among ``incomplete`` / ``done`` / ``unknown``. An archived conversation
is inspected but its recovery is never ``continuable``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .audit_classifier import SCORE_KEYS, Classification
from .audit_evidence import FINISHED, ConversationEvidence, MessageRef

SCHEMA_VERSION = 1
LOGIC_VERSION = "decision-v3"  # bumped whenever the precedence or a rule changes
FUTURE_SKEW_SECONDS = 60.0
MAX_REMAINING_STEPS = 5
MAX_STEP_CHARS = 160
MIN_HANDOFF_CHARS = 80
_STEP_HEADING = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?(?:remaining|next steps?|to-?do|left to do|still to do|outstanding)"
    r"\b[^\n]*$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?P<step>\S.*)$")


class ThresholdError(ValueError):
    """A threshold is outside its valid range or contradicts another."""


def _unit(name: str, value: object, *, open_low: bool = False) -> None:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ThresholdError(f"{name} must be a finite number")
    if not (0.0 < value <= 1.0 if open_low else 0.0 <= value <= 1.0):
        raise ThresholdError(f"{name} must be within {'(0, 1]' if open_low else '[0, 1]'}")


@dataclass(frozen=True)
class Thresholds:
    """Configurable and validated, never assumed perfect: ``audit_bench`` sweeps and records them."""

    done_min: float = 0.85  # task_finished must reach this for ``done``
    steps_max: float = 0.35  # steps_remaining must stay below this for ``done``
    blocked_veto: float = 0.60  # blocked_on_human at or above this withholds ``done`` (advisory)
    incomplete_steps_min: float = 0.60  # steps_remaining at or above this means ``incomplete``
    incomplete_done_max: float = 0.30  # task_finished at or below this means ``incomplete``
    max_age_seconds: float = 900.0
    unanswered_grace_seconds: float = 120.0
    stale_generation_seconds: float = 21600.0  # an in-progress turn older than this has stalled

    def __post_init__(self) -> None:
        _unit("done_min", self.done_min, open_low=True)
        _unit("steps_max", self.steps_max)
        _unit("blocked_veto", self.blocked_veto, open_low=True)
        _unit("incomplete_steps_min", self.incomplete_steps_min, open_low=True)
        _unit("incomplete_done_max", self.incomplete_done_max)
        if self.incomplete_done_max >= self.done_min:
            raise ThresholdError("incomplete_done_max must stay below done_min")
        if self.steps_max > self.incomplete_steps_min:
            raise ThresholdError("steps_max must not exceed incomplete_steps_min")
        for name in ("max_age_seconds", "unanswered_grace_seconds", "stale_generation_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ThresholdError(f"{name} must be a finite number")
        if self.max_age_seconds <= 0 or self.unanswered_grace_seconds < 0:
            raise ThresholdError("max_age_seconds must be > 0 and unanswered_grace_seconds >= 0")
        if self.stale_generation_seconds <= 0:
            raise ThresholdError("stale_generation_seconds must be > 0")

    def to_mapping(self) -> dict:
        return {
            "done_min": self.done_min,
            "steps_max": self.steps_max,
            "blocked_veto": self.blocked_veto,
            "incomplete_steps_min": self.incomplete_steps_min,
            "incomplete_done_max": self.incomplete_done_max,
            "max_age_seconds": self.max_age_seconds,
            "unanswered_grace_seconds": self.unanswered_grace_seconds,
            "stale_generation_seconds": self.stale_generation_seconds,
        }


DEFAULT_THRESHOLDS = Thresholds()


def thresholds_digest(thresholds: Thresholds) -> str:
    """Stable digest of the decision logic version and every threshold.

    A validation record or a cached verdict only applies to the exact logic and values it was
    produced under, so changing either invalidates both.
    """
    wire = json.dumps(
        {"logic": LOGIC_VERSION, "thresholds": thresholds.to_mapping()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(wire.encode()).hexdigest()


@dataclass(frozen=True)
class Verdict:
    response_state: str  # finished | generating | none | unknown
    task_state: str  # done | incomplete | blocked | working | unknown
    recovery_state: str  # not_needed | none | awaiting_owner | continuable | hold
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    remaining_steps: tuple[str, ...]
    scores: dict
    classifier_status: str
    model: str
    prompt_version: str
    schema_version: int = SCHEMA_VERSION
    classifier_meta: dict = field(
        default_factory=dict
    )  # checkpoint/max_len/truncated of an ok call

    def to_mapping(self, *, include_text: bool = False) -> dict:
        wire = {
            "response_state": self.response_state,
            "task_state": self.task_state,
            "recovery_state": self.recovery_state,
            "reasons": list(self.reasons),
            "evidence_ids": list(self.evidence_ids),
            "remaining_steps_count": len(self.remaining_steps),
            "scores": dict(self.scores),
            "classifier_status": self.classifier_status,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "classifier_meta": dict(self.classifier_meta),
        }
        if include_text:
            wire["remaining_steps"] = list(self.remaining_steps)
        return wire


def extract_remaining_steps(text: str) -> tuple[str, ...]:
    """Bullets listed under a "Remaining"/"Next steps"/"TODO" heading; bounded, text only."""
    steps: list[str] = []
    collecting = False
    for line in text.splitlines():
        if _STEP_HEADING.match(line):
            collecting = True
            continue
        if not collecting:
            continue
        bullet = _BULLET.match(line)
        if bullet:
            steps.append(bullet["step"].strip()[:MAX_STEP_CHARS])
            if len(steps) == MAX_REMAINING_STEPS:
                break
        elif line.strip():
            collecting = False
    return tuple(steps)


def _scores_valid(classification: Classification) -> bool:
    scores = classification.scores
    if not isinstance(scores, dict) or any(key not in scores for key in SCORE_KEYS):
        return False
    return all(
        type(scores[k]) in (int, float) and math.isfinite(scores[k]) and 0.0 <= scores[k] <= 1.0
        for k in SCORE_KEYS
    )


def _evidence_ids(e: ConversationEvidence) -> tuple[str, ...]:
    last_user = e.last_user
    candidates = (e.request_id, last_user.id if last_user else None, e.final_id)
    return tuple(dict.fromkeys(c for c in candidates if c is not None))


def _answers_latest_user(e: ConversationEvidence, final: MessageRef | None) -> bool:
    """The final answer sits AFTER the latest user message by position and, when known, by time."""
    if final is None:
        return False
    ids = [m.id for m in e.messages]
    user = e.last_user
    if user is None or ids.index(final.id) < ids.index(user.id):
        return False
    return not (
        user.created_at is not None
        and final.created_at is not None
        and final.created_at < user.created_at
    )


def _failure_after(e: ConversationEvidence, final: MessageRef) -> bool:
    after = e.messages[[m.id for m in e.messages].index(final.id) + 1 :]
    return any(m.role == "assistant" and m.status not in (FINISHED, "in_progress") for m in after)


def _current_turn(e: ConversationEvidence) -> tuple[MessageRef, ...]:
    """Messages after the latest user message: the only ones that can still be generating."""
    ids = [m.id for m in e.messages]
    user = e.last_user
    return e.messages[ids.index(user.id) + 1 :] if user is not None else e.messages


def _unresolved_in_progress(e: ConversationEvidence) -> list[MessageRef]:
    """Current-turn in-progress assistant messages with no finished final answer after them.

    A later finished final proves the turn completed, so an earlier async-status placeholder
    (in progress, weight 0 on the wire) must not veto it; one that comes AFTER the final is a
    fresh generation and still does.
    """
    turn = _current_turn(e)
    last_final = max(
        (
            i
            for i, m in enumerate(turn)
            if m.role == "assistant" and m.final and m.status == FINISHED
        ),
        default=-1,
    )
    return [m for m in turn[last_final + 1 :] if m.role == "assistant" and m.in_progress]


class _Lazy:
    """The classification, fetched at most once and only if a verdict actually depends on it."""

    def __init__(self, source: Classification | Callable[[], Classification | None] | None):
        self._source = source if callable(source) else None
        self.value = None if callable(source) else source
        self.requested = not callable(source)

    def get(self) -> Classification | None:
        if not self.requested:
            self.value, self.requested = self._source(), True
        return self.value

    @property
    def ok(self) -> bool:
        return self.value is not None and self.value.status == "ok"

    @property
    def scores(self) -> dict:
        return dict(self.value.scores) if self.ok and _scores_valid(self.value) else {}

    @property
    def meta(self) -> tuple[str, str, str]:
        if not self.requested:
            return ("not_needed", "", "")
        c = self.value
        return (c.status, c.model, c.prompt_version) if c is not None else ("unavailable", "", "")

    @property
    def route_meta(self) -> dict:
        """The validated route identity of an ok classification; empty for any other state."""
        if not self.ok or not isinstance(self.value.meta, dict):
            return {}
        meta = self.value.meta
        wanted = {k: meta[k] for k in ("checkpoint", "max_len", "truncated") if k in meta}
        return wanted if len(wanted) == 3 else {}


def _judge(laya: _Lazy, e: ConversationEvidence, thresholds: Thresholds, verdict) -> Verdict:
    """The model-assisted tail of the precedence: abstain, ``incomplete`` or ``done``, never blocked."""
    classification = laya.get()
    if classification is None or classification.status == "unavailable":
        return verdict("finished", "unknown", "hold", "classifier-unavailable")
    scores = laya.scores
    if not laya.ok or not scores:
        return verdict("finished", "unknown", "hold", "classifier-invalid")
    if scores["blocked_on_human"] >= thresholds.blocked_veto:
        # A model's guess that someone must act first is not evidence that anyone must: abstain.
        return verdict("finished", "unknown", "hold", "laya-blocked-advisory")
    if (
        scores["steps_remaining"] >= thresholds.incomplete_steps_min
        or scores["task_finished"] <= thresholds.incomplete_done_max
    ):
        steps = extract_remaining_steps(e.final_text)
        return verdict("finished", "incomplete", "continuable", "laya-incomplete", steps=steps)
    if (
        scores["task_finished"] >= thresholds.done_min
        and scores["steps_remaining"] < thresholds.steps_max
    ):
        return verdict("finished", "done", "not_needed", "laya-task-finished")
    return verdict("finished", "unknown", "hold", "classifier-ambiguous")


def decide(
    e: ConversationEvidence,
    classification: Classification | Callable[[], Classification | None] | None,
    *,
    now: float,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> Verdict:
    """The verdict for ``e``; ``classification`` may be a callable, invoked once and only when
    no deterministic rule has already decided, so a cheap read never costs a model call."""
    ids = _evidence_ids(e)
    laya = _Lazy(classification)

    def verdict(response: str, task: str, recovery: str, *reasons: str, steps=()) -> Verdict:
        if e.archived and recovery == "continuable":
            recovery, reasons = "hold", (*reasons, "archived-no-resume")
        return Verdict(
            response,
            task,
            recovery,
            tuple(reasons),
            ids,
            steps,
            laya.scores,
            *laya.meta,
            classifier_meta=laya.route_meta,
        )

    if e.observed_account is None:
        return verdict("unknown", "unknown", "hold", "account-missing")
    if e.observed_account != e.account:
        return verdict("unknown", "unknown", "hold", "account-mismatch")
    age = now - e.verified_at
    if age > thresholds.max_age_seconds or age < -FUTURE_SKEW_SECONDS:
        return verdict("unknown", "unknown", "hold", "stale-read")

    final = e.last_final
    user = e.last_user
    answered = _answers_latest_user(e, final)
    # An explicit stop/handoff request is honoured: what the assistant says in the handoff about
    # unfinished work or waiting is the handoff's content, not a reason to continue.
    handoff = (
        e.stop_intent
        and answered
        and final.status == FINISHED
        and len(e.final_text.strip()) >= MIN_HANDOFF_CHARS
        and not _failure_after(e, final)
    )

    if e.pending_approval and not handoff:
        return verdict("unknown", "blocked", "awaiting_owner", "pending-approval")
    if e.progress_indicator:
        return verdict("generating", "working", "none", "running")
    in_progress = _unresolved_in_progress(e)
    if in_progress:
        started = [m.created_at for m in in_progress if m.created_at is not None]
        if started and e.verified_at - max(started) > thresholds.stale_generation_seconds:
            return verdict("generating", "incomplete", "continuable", "stalled-generation")
        return verdict("generating", "working", "none", "in-progress-message")
    if e.context_truncated or e.request_truncated or e.final_truncated:
        return verdict("unknown", "unknown", "hold", "truncated-context")
    if e.request_id is None:
        return verdict("unknown", "unknown", "hold", "no-substantive-request")
    if not handoff:
        if e.permission_expired:
            return verdict("unknown", "blocked", "awaiting_owner", "permission-expired")
        if e.login_required:
            return verdict("unknown", "blocked", "awaiting_owner", "login-required")
        if e.human_input_required:
            return verdict("unknown", "blocked", "awaiting_owner", "human-input-required")

    if not answered:
        if e.end_turn_without_reply:
            return verdict("none", "incomplete", "continuable", "end-turn-without-reply")
        created = user.created_at if user is not None else None
        if created is None or e.verified_at - created < thresholds.unanswered_grace_seconds:
            return verdict("none", "unknown", "hold", "awaiting-first-response")
        return verdict("none", "incomplete", "continuable", "no-final-after-latest-user")
    if (
        user is not None
        and user.turn_uuid
        and final.turn_uuid
        and user.turn_uuid != final.turn_uuid
    ):
        return verdict("unknown", "unknown", "hold", "turn-mismatch")
    if final.status != FINISHED:
        return verdict("unknown", "incomplete", "continuable", "final-not-finished")
    if _failure_after(e, final):
        return verdict("finished", "incomplete", "continuable", "failure-after-final")
    if not e.final_text.strip():
        if final.artifacts > 0:
            # An image or file delivered with no text: the audit cannot read it, so it abstains
            # instead of calling a successful delivery incomplete (or an unreadable one done).
            return verdict("finished", "unknown", "hold", "artifact-only-final")
        return verdict("finished", "incomplete", "continuable", "final-has-no-answer-text")

    if handoff:
        classification = laya.get()
        if classification is None or classification.status == "unavailable":
            return verdict("finished", "unknown", "not_needed", "classifier-unavailable")
        if not laya.ok or not laya.scores:
            return verdict("finished", "unknown", "not_needed", "classifier-invalid")
        return verdict("finished", "done", "not_needed", "stop-intent-honored")
    if e.continuation_announced or e.unfinished_cue:
        return verdict(
            "finished",
            "incomplete",
            "continuable",
            e.unfinished_cue or "continuation-announced",
            steps=extract_remaining_steps(e.final_text),
        )
    if e.doubt_cue:
        return verdict("finished", "unknown", "hold", e.doubt_cue)
    return _judge(laya, e, thresholds, verdict)
