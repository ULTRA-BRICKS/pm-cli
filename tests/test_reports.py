"""Reports — queue / stuck / retro / monthly, each number traceable to item ids."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pmlib.events import EventKind, new_event
from pmlib.projection import build_intake_ledger, build_projection
from pmlib.reports import monthly_report, queue_report, retro_report, stuck_report
from pmlib.states import default_machine

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
NOW_TS = NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # matches events.py's _utc_now_iso() format
M = default_machine()


def _project(events: list) -> dict:
    return build_projection(events, M)


def _intake(iid: str, title: str = "t") -> object:
    return new_event(EventKind.INTAKE, "", {"id": iid, "title": title})


def _to(iid: str, target: str) -> object:
    return new_event(EventKind.TRANSITION, iid, {"to": target})


# ── queue ────────────────────────────────────────────────────────────────────────────────
def test_queue_report_groups_by_category_with_ids() -> None:
    items = _project([_intake("a"), _intake("b"), _to("b", "classified"), _to("b", "ticketed")])
    rep = queue_report(items, M).to_dict()
    assert rep["total"] == 2
    intake = next(s for s in rep["groups"]["by_category"] if s["label"] == "intake")
    assert intake["count"] == 2 and set(intake["item_ids"]) == {"a", "b"}


# ── stuck ────────────────────────────────────────────────────────────────────────────────
def test_stuck_report_flags_exception_state_and_deadletter() -> None:
    events = [_intake("x"), _to("x", "classified"), _to("x", "needs-clarification")]
    # walk x into a stuck (exception) state via a legal-ish path? use human-required directly:
    events = [
        _intake("s"),
        _to("s", "classified"),
        _to("s", "ticketed"),
        _to("s", "ready"),
        _to("s", "blocked-by-dependency"),
    ]
    events.append(new_event(EventKind.INTAKE_CAPTURED, "", {"id": "tg:1", "text": "boom"}))
    events.append(new_event(EventKind.INTAKE_DEADLETTER, "tg:1", {"reason": "gave up"}))
    items = build_projection(events, M)
    ledger = build_intake_ledger(events)
    rep = stuck_report(items, M, NOW, ledger).to_dict()
    kinds = {(e["kind"], e["item_id"]) for e in rep["entries"]}
    assert ("blocked", "s") in kinds  # blocked-by-dependency is a blocked state
    assert ("deadletter", "tg:1") in kinds


def test_stuck_report_flags_no_progress() -> None:
    old = (NOW - timedelta(days=3)).isoformat()
    ev = [
        _intake("p"),
        _to("p", "classified"),
        _to("p", "ticketed"),
        _to("p", "ready"),
        _to("p", "scoped"),
        _to("p", "queued"),
        _to("p", "dispatched"),
        _to("p", "implementing"),
    ]
    items = build_projection(ev, M)
    items["p"].updated_at = old  # force staleness
    rep = stuck_report(items, M, NOW, {}).to_dict()
    assert any(e["kind"] == "no-progress" and e["item_id"] == "p" for e in rep["entries"])


# ── retro ────────────────────────────────────────────────────────────────────────────────
def test_retro_report_captures_trigger_and_recovery() -> None:
    # a -> ... -> stuck -> ready (recovered)
    ev = [
        _intake("a"),
        _to("a", "classified"),
        _to("a", "ticketed"),
        _to("a", "ready"),
        _to("a", "scoped"),
        _to("a", "queued"),
        _to("a", "dispatched"),
        _to("a", "implementing"),
        _to("a", "reviewing"),
        _to("a", "fixing"),
    ]
    # move to stuck is not legal from fixing; use a direct exception path: dispatched->... skip.
    # Simpler: intake -> classified -> ... -> stuck via ready? ready has no stuck edge. Use
    # human-required reachable from stuck only. Build a minimal exception pass with config-drift:
    ev = [
        _intake("a"),
        _to("a", "classified"),
        _to("a", "ticketed"),
        _to("a", "ready"),
        _to("a", "blocked-by-dependency"),
        _to("a", "ready"),
    ]  # blocked (exception-ish) then out
    items = build_projection(ev, M)
    rep = retro_report(items, M, NOW).to_dict()
    entry = next(e for e in rep["entries"] if e["item_id"] == "a")
    assert entry["trigger_state"] == "blocked-by-dependency"
    assert entry["recovered"] is True


def test_retro_report_counts_recurring_patterns() -> None:
    def stuck_pass(iid: str) -> list:
        return [
            _intake(iid),
            _to(iid, "classified"),
            _to(iid, "ticketed"),
            _to(iid, "ready"),
            _to(iid, "blocked-by-dependency"),
        ]

    items = build_projection(stuck_pass("a") + stuck_pass("b"), M)
    rep = retro_report(items, M, NOW).to_dict()
    pat = next(p for p in rep["patterns"] if p["state"] == "blocked-by-dependency")
    assert pat["count"] == 2 and set(pat["item_ids"]) == {"a", "b"}


# ── monthly ──────────────────────────────────────────────────────────────────────────────
def test_monthly_report_groups_product_and_process() -> None:
    # pm-cli#13: pin every event to NOW (2026-07) instead of new_event()'s real wall-clock
    # stamp -- this test hardcodes the "2026-07" report window it queries below, so leaving
    # events real-time-stamped made it a time bomb that silently broke the moment real time
    # crossed into a new month.
    ev = [
        replace(e, ts=NOW_TS)
        for e in (
            _intake("a"),
            _to("a", "classified"),
            _to("a", "ticketed"),
            _to("a", "ready"),
            _to("a", "scoped"),
            _to("a", "queued"),
            _to("a", "dispatched"),
            _to("a", "implementing"),
            _to("a", "reviewing"),
            _to("a", "verifying"),
            _to("a", "pr-open"),
            _to("a", "ci-pending"),
            _to("a", "ship-ready"),
            new_event(EventKind.INTAKE_CAPTURED, "", {"id": "tg:1", "text": "x"}),
            new_event(EventKind.INTAKE_DEADLETTER, "tg:1", {"reason": "boom"}),
        )
    ]
    items = build_projection(ev, M)
    rep = monthly_report(ev, items, M, "2026-07").to_dict()
    product = {s["label"]: s for s in rep["groups"]["product"]}
    process = {s["label"]: s for s in rep["groups"]["process"]}
    assert product["shipped"]["count"] == 1 and product["shipped"]["item_ids"] == ["a"]
    assert process["deadletter"]["count"] == 1


def test_monthly_report_excludes_other_months() -> None:
    ev = [_intake("a"), _to("a", "classified")]
    items = build_projection(ev, M)
    rep = monthly_report(ev, items, M, "2020-01").to_dict()  # nothing happened in 2020
    assert all(s["count"] == 0 for group in rep["groups"].values() for s in group)
