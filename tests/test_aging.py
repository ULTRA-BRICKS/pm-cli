"""Aging report — current-state age past per-state SLA, distinct from stuck.

Accessed via: ``pm report aging`` -> ``aging_report``. Reuses ``current_state_age_seconds``
from the metrics dwell helper (does not reimplement). Healthy states can age; terminal
states (and deployed) do not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmlib.model import Transition, WorkItem
from pmlib.reminders import DEFAULT_NO_PROGRESS_SECONDS
from pmlib.reports import aging_report, current_state_age_seconds
from pmlib.states import StateMachine, WorkStateCategory, WorkStateDefinition, default_machine

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
M = default_machine()


def _iso(seconds_before_now: int) -> str:
    return (NOW - timedelta(seconds=seconds_before_now)).isoformat()


def _item(iid: str, state: str, *, age: int, history: bool = True) -> WorkItem:
    ts = _iso(age)
    hist = [Transition("dispatched", state, ts)] if history else []
    created = _iso(age + 100) if history else ts
    return WorkItem(id=iid, state=state, created_at=created, updated_at=ts, history=hist)


def test_aging_flags_healthy_state_past_default_sla() -> None:
    item = _item("slow", "implementing", age=DEFAULT_NO_PROGRESS_SECONDS + 10)
    assert current_state_age_seconds(item, NOW) == DEFAULT_NO_PROGRESS_SECONDS + 10
    rep = aging_report({"slow": item}, M, NOW).to_dict()
    assert rep["kind"] == "aging"
    assert any(e["item_id"] == "slow" and e["age_seconds"] == DEFAULT_NO_PROGRESS_SECONDS + 10
               for e in rep["entries"])


def test_aging_skips_fresh_item() -> None:
    item = _item("fresh", "implementing", age=60)
    ids = {e["item_id"] for e in aging_report({"fresh": item}, M, NOW).to_dict()["entries"]}
    assert "fresh" not in ids


def test_aging_skips_terminal_and_deployed() -> None:
    items = {
        "done": _item("done", "done", age=DEFAULT_NO_PROGRESS_SECONDS * 10),
        "parked": _item("parked", "parked", age=DEFAULT_NO_PROGRESS_SECONDS * 10),
        "cancelled": _item("cancelled", "cancelled", age=DEFAULT_NO_PROGRESS_SECONDS * 10),
        "deployed": _item("deployed", "deployed", age=DEFAULT_NO_PROGRESS_SECONDS * 10),
    }
    # deployed history from dispatched is illegal-looking but we construct directly
    items["deployed"] = WorkItem(
        id="deployed", state="deployed", created_at=_iso(10**6),
        updated_at=_iso(DEFAULT_NO_PROGRESS_SECONDS * 10),
        history=[Transition("ship-ready", "deployed", _iso(DEFAULT_NO_PROGRESS_SECONDS * 10))],
    )
    assert aging_report(items, M, NOW).to_dict()["entries"] == []


def test_aging_uses_created_at_when_history_empty() -> None:
    item = _item("nohist", "ready", age=DEFAULT_NO_PROGRESS_SECONDS + 5, history=False)
    entry = next(e for e in aging_report({"nohist": item}, M, NOW).to_dict()["entries"])
    assert entry["item_id"] == "nohist"
    assert entry["age_seconds"] == DEFAULT_NO_PROGRESS_SECONDS + 5


def test_aging_honors_per_state_sla_seconds() -> None:
    machine = StateMachine({
        "busy": WorkStateDefinition(
            "busy", WorkStateCategory.ACTIVE, allowed_next=[], sla_seconds=10),
    }, initial_state="busy")
    stale = WorkItem(
        id="Q", state="busy", created_at=_iso(100),
        history=[Transition("ready", "busy", _iso(60))],
    )
    fresh = WorkItem(
        id="F", state="busy", created_at=_iso(100),
        history=[Transition("ready", "busy", _iso(5))],
    )
    ids = {e["item_id"] for e in aging_report({"Q": stale, "F": fresh}, machine, NOW).to_dict()["entries"]}
    assert ids == {"Q"}
