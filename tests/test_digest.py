"""Weekly digest body copies metrics/aging/queue numbers — no separate computation path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmlib.digest import render_weekly_digest, shipped_ids_since, weekly_digest_html
from pmlib.events import EventKind, new_event
from pmlib.model import Transition, WorkItem
from pmlib.reports import aging_report, metrics_report, queue_report
from pmlib.states import default_machine

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
M = default_machine()


def _iso(seconds_before_now: int) -> str:
    return (NOW - timedelta(seconds=seconds_before_now)).isoformat()


def test_digest_body_matches_metrics_report_numbers() -> None:
    item = WorkItem(
        id="re",
        state="stuck",
        created_at=_iso(10_000),
        history=[
            Transition("ready", "stuck", _iso(300)),
            Transition("stuck", "ready", _iso(200)),
            Transition("ready", "stuck", _iso(150)),
        ],
    )
    items = {"re": item}
    metrics = metrics_report(items, M, NOW).to_dict()
    aging = aging_report(items, M, NOW).to_dict()
    queue = queue_report(items, M).to_dict()
    exception = next(s for s in metrics["groups"]["by_category"] if s["label"] == "exception")
    body = render_weekly_digest(metrics, aging, queue, shipped_ids=["ship-1"])
    assert f"mean {exception['mean_seconds']}s" in body
    assert f"p50 {exception['p50_seconds']}s" in body
    assert f"p90 {exception['p90_seconds']}s" in body
    assert "ship-1" in body
    wip = next(s for s in queue["groups"]["by_category"] if s["label"] == "exception")
    assert f"exception: {wip['count']}" in body


def test_weekly_digest_html_matches_report_functions() -> None:
    item = WorkItem(
        id="re",
        state="implementing",
        created_at=_iso(10_000),
        history=[Transition("dispatched", "implementing", _iso(200))],
    )
    items = {"re": item}
    body = weekly_digest_html(items, [], M, NOW)
    metrics = metrics_report(items, M, NOW).to_dict()
    active = next(s for s in metrics["groups"]["by_category"] if s["label"] == "active")
    assert f"mean {active['mean_seconds']}s" in body


def test_shipped_ids_since_uses_transition_events() -> None:
    from dataclasses import replace
    ev = replace(new_event(EventKind.TRANSITION, "a", {"to": "done"}), ts=_iso(3600))
    old = replace(new_event(EventKind.TRANSITION, "b", {"to": "done"}), ts=_iso(8 * 86400))
    assert shipped_ids_since([ev, old], NOW, days=7) == ["a"]
