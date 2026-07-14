"""The ``WorkItem`` — the projected shape a queue row takes, folded from events.

Pure data, no I/O. ``projection.py`` builds these by replaying the event log; the CLI renders
them. A subset of research #255's ``WorkItem`` TS interface: the fields Release 1 (the "organ
of sight") populates from observation events. Dependency edges, timers, owner/executor refs are
declared where they belong in the design but arrive in later milestones — this milestone is the
event-sourced spine, not the whole graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Privacy class gates later dispatch (#255: local-only work must never reach a remote model).
# Declared here so it is recorded from intake onward, even though enforcement lands with dispatch.
PRIVACY_LOCAL_ONLY = "local-only"
PRIVACY_REMOTE_ALLOWED = "remote-allowed"


@dataclass
class EvidenceRef:
    """A piece of evidence backing a state — a PR URL, a CI result, a ship-audit row, etc."""

    kind: str
    uri: str
    observed_at: str
    digest: str = ""


@dataclass
class Transition:
    """One recorded state move, for the item's history / the ``pm why`` explanation."""

    from_state: str
    to: str
    ts: str


# The five typed dependency edges (research #255). An edge lives on the SOURCE item and points at
# the target: ``X blocks Y`` means X must finish before Y proceeds; ``X waits_for Y`` means X waits
# on Y; ``duplicates``/``supersedes`` relate near-identical work; ``relates_to`` is context only.
EDGE_TYPES = ("blocks", "waits_for", "duplicates", "supersedes", "relates_to")


@dataclass
class DependencyEdge:
    """A typed relationship from this item to ``target_id`` (see :data:`EDGE_TYPES`)."""

    edge_type: str
    target_id: str
    reason: str = ""
    condition: dict = field(default_factory=dict)  # optional WaitCondition for waits_for edges


@dataclass
class TimerRef:
    """A scheduled reminder/SLA/quota timer on an item; fires when ``due_at`` passes."""

    kind: str  # sla | retry | quota-reset | review-dwell | reminder
    due_at: str  # ISO-8601
    reason: str = ""


@dataclass
class WorkItem:
    """A queue row: identity, current state, accumulated evidence, and its history."""

    id: str
    title: str = ""
    state: str = "seen"
    project_id: str = ""
    privacy: str = PRIVACY_LOCAL_ONLY
    task_refs: list[str] = field(default_factory=list)
    intake_refs: list[str] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    history: list[Transition] = field(default_factory=list)
    dependencies: list[DependencyEdge] = field(default_factory=list)
    timers: list[TimerRef] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # rejected/logged inconsistencies
    created_at: str = ""
    updated_at: str = ""

    def evidence_kinds(self) -> set[str]:
        """The set of evidence kinds accumulated so far (feeds the FSM evidence gate)."""
        return {e.kind for e in self.evidence}

    def to_dict(self) -> dict:
        """A JSON-serializable view for ``pm queue --json`` and reports."""
        return {
            "id": self.id, "title": self.title, "state": self.state,
            "project": self.project_id, "privacy": self.privacy,
            "task_refs": list(self.task_refs), "intake_refs": list(self.intake_refs),
            "evidence": [{"kind": e.kind, "uri": e.uri, "observed_at": e.observed_at}
                         for e in self.evidence],
            "history": [{"from": h.from_state, "to": h.to, "ts": h.ts} for h in self.history],
            "dependencies": [{"type": d.edge_type, "target_id": d.target_id,
                              "reason": d.reason, "condition": dict(d.condition)}
                             for d in self.dependencies],
            "timers": [{"kind": t.kind, "due_at": t.due_at, "reason": t.reason}
                       for t in self.timers],
            "errors": list(self.errors),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }
