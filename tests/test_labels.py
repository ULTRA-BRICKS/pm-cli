"""The PM projection labels (pm.stage / pm.blocked / pm.health) — labels-only, task-safe."""

from __future__ import annotations

from pmlib.labels import pm_labels
from pmlib.model import WorkItem
from pmlib.states import default_machine


def _labels(state: str) -> list[str]:
    return pm_labels(WorkItem(id="w1", state=state), default_machine())


def test_healthy_active_item() -> None:
    labels = _labels("implementing")
    assert "pm.stage:active" in labels
    assert "pm.health:healthy" in labels
    assert not any(x.startswith("pm.blocked:") for x in labels)


def test_blocked_item_gets_blocked_and_health_labels() -> None:
    labels = _labels("blocked-by-dependency")
    assert "pm.blocked:blocked-by-dependency" in labels
    assert "pm.health:blocked" in labels


def test_transient_wait_is_waiting_not_blocked() -> None:
    """limit-wait is a transient exception (quota reset), NOT a blocker — so no pm.blocked label."""
    labels = _labels("limit-wait")
    assert "pm.stage:exception" in labels
    assert "pm.health:waiting" in labels
    assert not any(x.startswith("pm.blocked:") for x in labels)


def test_post_states_report_their_own_health() -> None:
    assert "pm.health:parked" in _labels("parked")
    assert "pm.health:superseded" in _labels("superseded")
    assert "pm.health:cancelled" in _labels("cancelled")


def test_stuck_state_reports_stuck_health_and_is_blocked() -> None:
    labels = _labels("stuck")
    assert "pm.health:stuck" in labels  # stuck is a MORE specific health than generic blocked
    assert "pm.blocked:stuck" in labels  # ...and it is a blocked/exception state
    assert "pm.stage:exception" in labels


def test_done_terminal_health() -> None:
    assert "pm.health:done" in _labels("done")


def test_unknown_state_is_marked_unknown_not_crashing() -> None:
    assert "pm.stage:unknown" in _labels("nonesuch")


def test_unknown_state_gets_no_blocked_label() -> None:
    """An unknown ``blocked-*`` state must not claim ``pm.blocked`` — we can't assert it's blocked."""
    labels = _labels("blocked-foo")  # not a real state in the machine
    assert not any(x.startswith("pm.blocked:") for x in labels)
    assert "pm.health:unknown" in labels


def test_pm_health_helper_matches_label() -> None:
    from pmlib.labels import pm_health

    assert pm_health(WorkItem(id="w", state="implementing"), default_machine()) == "healthy"
