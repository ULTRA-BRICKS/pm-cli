"""Time-in-status metrics — per-category dwell from consecutive transition timestamps.

Accessed via: ``pm report metrics`` -> ``metrics_report`` (pure; clock passed in).
Re-entries accumulate: stuck -> ready -> stuck sums both exception visits on the same item.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pmlib.model import Transition, WorkItem
from pmlib.reports import metrics_report
from pmlib.states import default_machine

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
M = default_machine()


def _iso(seconds_before_now: int) -> str:
    return (NOW - timedelta(seconds=seconds_before_now)).isoformat()


def _item(iid: str, state: str, history: list[Transition]) -> WorkItem:
    last_ts = history[-1].ts if history else _iso(0)
    return WorkItem(id=iid, state=state, created_at=_iso(10_000), updated_at=last_ts, history=history)


def _section(rep: dict, label: str) -> dict:
    return next(s for s in rep["groups"]["by_category"] if s["label"] == label)


def test_reentry_accumulates_time_in_category() -> None:
    """stuck -> ready -> stuck: both exception visits sum; triage in between is separate."""
    # T-300 ready->stuck (100s exception), T-200 stuck->ready (50s triage),
    # T-150 ready->stuck (150s exception, still current at NOW).
    item = _item("re", "stuck", [
        Transition("ready", "stuck", _iso(300)),
        Transition("stuck", "ready", _iso(200)),
        Transition("ready", "stuck", _iso(150)),
    ])
    rep = metrics_report({"re": item}, M, NOW).to_dict()
    assert rep["kind"] == "metrics"
    exception = _section(rep, "exception")
    triage = _section(rep, "triage")
    assert exception["item_ids"] == ["re"]
    assert exception["mean_seconds"] == 250  # 100 + 150
    assert exception["p50_seconds"] == 250
    assert exception["p90_seconds"] == 250
    assert triage["mean_seconds"] == 50
    assert "intake" not in {s["label"] for s in rep["groups"]["by_category"]}


def test_percentiles_linear_interpolation_across_items() -> None:
    """p50/p90 use linear interpolation between closest ranks (Hyndman & Fan type 7)."""
    # Five items that each spent a single known dwell in exception.
    items = {}
    for i, dwell in enumerate((10, 20, 30, 40, 50), start=1):
        items[str(i)] = _item(str(i), "stuck", [
            Transition("ready", "stuck", _iso(dwell)),
        ])
    rep = metrics_report(items, M, NOW).to_dict()
    exception = _section(rep, "exception")
    assert exception["mean_seconds"] == 30
    assert exception["p50_seconds"] == 30
    assert exception["p90_seconds"] == 46  # k=4*0.9=3.6 -> 40 + 0.6*(50-40)
    assert exception["count"] == 5
    assert set(exception["item_ids"]) == {"1", "2", "3", "4", "5"}


def test_zero_dwell_category_omitted() -> None:
    item = _item("only-triage", "ready", [
        Transition("ticketed", "ready", _iso(40)),
    ])
    labels = {s["label"] for s in metrics_report({"only-triage": item}, M, NOW).to_dict()["groups"]["by_category"]}
    assert labels == {"triage"}
