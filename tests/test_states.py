"""The configurable FSM: default registry integrity, adjacency, evidence gates."""

from __future__ import annotations

import pytest

from pmlib.states import (
    DEFAULT_STATES,
    EvidenceError,
    StateMachine,
    TransitionError,
    WorkStateCategory,
    WorkStateDefinition,
    default_machine,
)


def test_default_registry_is_internally_consistent() -> None:
    """Every ``allowed_next`` target and the initial state must be a defined state."""
    ids = set(DEFAULT_STATES)
    for defn in DEFAULT_STATES.values():
        for nxt in defn.allowed_next:
            assert nxt in ids, f"{defn.id} -> unknown state {nxt}"
        if defn.terminal:
            assert not defn.allowed_next, f"terminal {defn.id} must have no exits"


def test_every_category_is_represented() -> None:
    seen = {d.category for d in DEFAULT_STATES.values()}
    assert seen == set(WorkStateCategory)


def test_legal_transition_passes() -> None:
    m = default_machine()
    m.validate_transition("seen", "classified", evidence_kinds=set())  # no raise


def test_illegal_transition_raises() -> None:
    m = default_machine()
    with pytest.raises(TransitionError):
        m.validate_transition("seen", "deployed", evidence_kinds=set())


def test_force_bypasses_illegal_transition() -> None:
    m = default_machine()
    m.validate_transition("seen", "deployed", evidence_kinds=set(), force=True)  # no raise


def test_evidence_gate_blocks_when_missing() -> None:
    m = _machine_with_gate()
    with pytest.raises(EvidenceError):
        m.validate_transition("a", "b", evidence_kinds=set())


def test_evidence_gate_passes_when_present() -> None:
    m = _machine_with_gate()
    m.validate_transition("a", "b", evidence_kinds={"ci"})  # no raise


def test_evidence_gate_bypassed_by_force() -> None:
    m = _machine_with_gate()
    m.validate_transition("a", "b", evidence_kinds=set(), force=True)  # no raise


def test_unknown_state_raises() -> None:
    m = default_machine()
    with pytest.raises(TransitionError):
        m.validate_transition("seen", "nonesuch", evidence_kinds=set())


def test_fsm_error_exposes_single_line_summary() -> None:
    """The projection records ``.summary`` — it must be the first line only, no prose spill."""
    m = default_machine()
    try:
        m.validate_transition("seen", "deployed", evidence_kinds=set())
    except TransitionError as exc:
        assert "\n" not in exc.summary
        assert exc.summary.startswith("illegal transition")
    else:  # pragma: no cover
        raise AssertionError("expected TransitionError")


def _machine_with_gate() -> StateMachine:
    return StateMachine(
        {
            "a": WorkStateDefinition("a", WorkStateCategory.PLANNED, allowed_next=["b"]),
            "b": WorkStateDefinition(
                "b", WorkStateCategory.DELIVERY, allowed_next=[], evidence_required=["ci"]
            ),
        }
    )
