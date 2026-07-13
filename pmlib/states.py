"""The configurable work-state FSM — categories, definitions, adjacency, evidence gates.

WHAT THIS FILE IS
    ``WorkStateCategory`` + ``WorkStateDefinition`` + ``StateMachine`` (the validator) + the
    ``DEFAULT_STATES`` registry. Research #255 is explicit: DO NOT hard-code task-cli's five
    states as the PM model — the PM machine is configurable, with categories and required
    evidence, and task-cli states are just one projection. So the machine is data-driven: a
    dict of definitions, validated for internal consistency.

INVARIANTS
    - A transition is legal iff ``target in current.allowed_next``.
    - Entering a state whose ``evidence_required`` is not satisfied by the item's accumulated
      evidence kinds is refused — terminal/delivery moves must be backed by evidence the PM
      cannot forge (#255: ``done``/``deployed`` need external facts). ``force`` bypasses both
      checks (the audited operator override).
    - The registry is self-consistent: every ``allowed_next`` target exists, terminal states
      have no exits (asserted by ``tests/test_states.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Structured exit codes (stable across the ecosystem; 2 == "invalid request", the task-cli
# EXIT_USAGE convention). Evidence failures get their own code so a script can branch on them.
EXIT_ILLEGAL_TRANSITION = 2
EXIT_MISSING_EVIDENCE = 5


class WorkStateCategory(str, Enum):
    """The seven lifecycle phases from research #255's flexible-FSM section."""

    INTAKE = "intake"
    TRIAGE = "triage"
    PLANNED = "planned"
    ACTIVE = "active"
    DELIVERY = "delivery"
    POST = "post"
    EXCEPTION = "exception"


@dataclass(frozen=True)
class WorkStateDefinition:
    """One state: its category, exits, evidence gate, and how it projects onto task-cli."""

    id: str
    category: WorkStateCategory
    allowed_next: list[str] = field(default_factory=list)
    terminal: bool = False
    evidence_required: list[str] = field(default_factory=list)
    owner_required: str = "none"  # human | pm-agent | subagent | external-ci | none
    sla_seconds: int | None = None
    task_projection: str | None = None  # todo | in-progress | in-review | done | cancelled
    # Structural blocked flag — an item in this state is stalled on a blocker (a decision, a
    # dependency, a missing harness, a human). Declared per state rather than inferred from the
    # id prefix or the category, so a transient WAIT (limit-wait / provider-overload) is NOT
    # mislabelled blocked. Drives the pm.blocked projection label.
    blocked: bool = False


class _FsmError(Exception):
    """Base for FSM rejections. Carries a one-line ``summary`` so callers (the projection's
    ``item.errors`` log) never have to re-parse the multi-line human message to get the gist.
    """

    exit_code: int = EXIT_ILLEGAL_TRANSITION

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.summary = message.splitlines()[0]


class TransitionError(_FsmError):
    """An illegal state move (unknown state, or target not in ``allowed_next``)."""

    exit_code: int = EXIT_ILLEGAL_TRANSITION


class EvidenceError(_FsmError):
    """The target state's required evidence is not present on the item."""

    exit_code: int = EXIT_MISSING_EVIDENCE


class StateMachine:
    """A validated set of :class:`WorkStateDefinition` s + the one transition validator."""

    def __init__(self, states: dict[str, WorkStateDefinition], initial_state: str = "seen") -> None:
        self.states = states
        self.initial_state = initial_state

    def get(self, state_id: str) -> WorkStateDefinition | None:
        return self.states.get(state_id)

    def validate_transition(
        self, current: str, target: str, *, evidence_kinds: set[str], force: bool = False
    ) -> None:
        """Raise if ``current -> target`` is illegal or the target's evidence gate is unmet."""
        if force:
            return
        cur = self.states.get(current)
        if cur is None:
            raise TransitionError(_unknown_state_msg(current, "current"))
        tgt = self.states.get(target)
        if tgt is None:
            raise TransitionError(_unknown_state_msg(target, "target"))
        if target not in cur.allowed_next:
            raise TransitionError(_illegal_msg(current, target, cur.allowed_next))
        missing = [k for k in tgt.evidence_required if k not in evidence_kinds]
        if missing:
            raise EvidenceError(_missing_evidence_msg(target, missing))


def _unknown_state_msg(state: str, role: str) -> str:
    return (
        f"unknown {role} state `{state}`\n"
        f"  why: it is not defined in the state machine\n"
        f"  fix: use one of the configured states (see `pm why <id>`)"
    )


def _illegal_msg(current: str, target: str, allowed: list[str]) -> str:
    nxt = ", ".join(allowed) or "(none — terminal)"
    return (
        f"illegal transition `{current}` -> `{target}`\n"
        f"  why: `{target}` is not a legal next state from `{current}`\n"
        f"  fix: move to one of [{nxt}], or pass --force to override"
    )


def _missing_evidence_msg(target: str, missing: list[str]) -> str:
    kinds = ", ".join(missing)
    return (
        f"missing evidence for `{target}`: {kinds}\n"
        f"  why: this state requires unforgeable evidence before the item may enter it\n"
        f"  fix: record it (`pm record evidence <id> <kind> <uri>`) or pass --force"
    )


# ── the default registry ─────────────────────────────────────────────────────────────────
# A dense data table (kept as one block per the repo convention for tables): each row is a
# state, its category, and its legal exits. Evidence gates live on the terminal delivery
# states (#255: `deployed` needs a real deployment probe). This is the DEFAULT projection;
# a deployment can supply its own registry.
def _def(
    id_: str,
    cat: WorkStateCategory,
    nxt: list[str],
    *,
    terminal: bool = False,
    evidence_required: list[str] | None = None,
    task_projection: str | None = None,
    owner_required: str = "none",
    sla_seconds: int | None = None,
    blocked: bool = False,
) -> WorkStateDefinition:
    return WorkStateDefinition(
        id=id_,
        category=cat,
        allowed_next=nxt,
        terminal=terminal,
        evidence_required=list(evidence_required or []),
        task_projection=task_projection,
        owner_required=owner_required,
        sla_seconds=sla_seconds,
        blocked=blocked,
    )


_C = WorkStateCategory
DEFAULT_STATES: dict[str, WorkStateDefinition] = {
    # intake
    "seen": _def("seen", _C.INTAKE, ["classified", "deduped"]),
    "classified": _def("classified", _C.INTAKE, ["ticketed", "needs-clarification"]),
    "deduped": _def("deduped", _C.INTAKE, ["superseded", "ticketed"]),
    "ticketed": _def(
        "ticketed",
        _C.INTAKE,
        ["ready", "blocked-by-decision", "blocked-by-dependency"],
        task_projection="todo",
    ),
    # triage
    "needs-clarification": _def("needs-clarification", _C.TRIAGE, ["ready", "cancelled"]),
    "ready": _def("ready", _C.TRIAGE, ["scoped", "blocked-by-dependency"], task_projection="todo"),
    "blocked-by-decision": _def(
        "blocked-by-decision", _C.TRIAGE, ["ready", "cancelled"], blocked=True
    ),
    "blocked-by-dependency": _def("blocked-by-dependency", _C.TRIAGE, ["ready"], blocked=True),
    # planned
    "scoped": _def("scoped", _C.PLANNED, ["queued"]),
    "queued": _def("queued", _C.PLANNED, ["dispatched", "scheduled"]),
    "scheduled": _def("scheduled", _C.PLANNED, ["dispatched"]),
    # active
    "dispatched": _def("dispatched", _C.ACTIVE, ["implementing"], task_projection="in-progress"),
    "implementing": _def(
        "implementing", _C.ACTIVE, ["reviewing", "fixing"], task_projection="in-progress"
    ),
    "reviewing": _def("reviewing", _C.ACTIVE, ["fixing", "verifying"], task_projection="in-review"),
    "fixing": _def("fixing", _C.ACTIVE, ["reviewing", "verifying"], task_projection="in-progress"),
    "verifying": _def("verifying", _C.ACTIVE, ["pr-open"], task_projection="in-review"),
    # delivery
    "pr-open": _def("pr-open", _C.DELIVERY, ["ci-pending"], task_projection="in-review"),
    "ci-pending": _def("ci-pending", _C.DELIVERY, ["ship-ready", "fixing"]),
    "ship-ready": _def("ship-ready", _C.DELIVERY, ["deployed"]),
    "deployed": _def(
        "deployed", _C.DELIVERY, ["done", "deploy-verifying"], evidence_required=["deployment"]
    ),
    "deploy-verifying": _def("deploy-verifying", _C.DELIVERY, ["done"]),
    # post
    "done": _def("done", _C.POST, [], terminal=True, task_projection="done"),
    "parked": _def("parked", _C.POST, [], terminal=True),
    "cancelled": _def("cancelled", _C.POST, [], terminal=True, task_projection="cancelled"),
    "superseded": _def("superseded", _C.POST, [], terminal=True),
    "needs-retro": _def("needs-retro", _C.POST, ["retro-done"]),
    "retro-done": _def("retro-done", _C.POST, [], terminal=True),
    # exception — `blocked` states are stalled on a blocker; limit-wait / provider-overload are
    # transient WAITS (progress resumes on quota reset / backoff), so they are NOT blocked.
    "stuck": _def(
        "stuck", _C.EXCEPTION, ["ready", "dispatched", "parked", "human-required"], blocked=True
    ),
    "limit-wait": _def("limit-wait", _C.EXCEPTION, ["dispatched", "queued"]),
    "provider-overload": _def("provider-overload", _C.EXCEPTION, ["dispatched", "queued"]),
    "harness-missing": _def(
        "harness-missing", _C.EXCEPTION, ["queued", "human-required"], blocked=True
    ),
    "config-drift": _def("config-drift", _C.EXCEPTION, ["ready", "human-required"], blocked=True),
    "human-required": _def("human-required", _C.EXCEPTION, ["ready", "cancelled"], blocked=True),
}


def default_machine() -> StateMachine:
    """The default PM state machine (a copy of the shared registry, seeded at ``seen``)."""
    return StateMachine(dict(DEFAULT_STATES), initial_state="seen")
