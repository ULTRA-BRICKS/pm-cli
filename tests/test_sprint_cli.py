"""``pm sprint`` end-to-end via the CLI entry point."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pmlib.cli import main

_SPRINT_ID_RE = re.compile(r"^sprint:[0-9a-f]{10}$")


def _run(*argv: str) -> int:
    return main(list(argv))


def _create(capsys, name: str = "S1", goal: str = "") -> str:
    argv = ["sprint", "create", "--name", name]
    if goal:
        argv += ["--goal", goal]
    assert _run(*argv) == 0
    sprint_id = capsys.readouterr().out.strip()
    assert _SPRINT_ID_RE.match(sprint_id)
    return sprint_id


def _item(iid: str, capsys) -> None:
    _run("record", "intake", "--id", iid, "--title", iid)
    capsys.readouterr()


def test_create_prints_a_fresh_sprint_id(pm_home: Path, capsys) -> None:
    _create(capsys, name="Sprint 1", goal="ship the thing")
    assert _run("sprint", "list") == 0
    out = capsys.readouterr().out
    assert "Sprint 1" in out
    assert "planning" in out


def test_add_pulls_an_item_in_with_an_estimate(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    assert _run("sprint", "add", sid, "w1", "--estimate", "3") == 0
    capsys.readouterr()
    assert _run("sprint", "status", sid, "--json") == 0
    status = json.loads(capsys.readouterr().out)
    assert "items" not in status["sprint"]  # superseded by "rows" — never both in the same payload
    assert status["rows"][0]["item_id"] == "w1"
    assert status["rows"][0]["estimate"] == 3.0


def test_add_rejects_unknown_work_item(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    rc = _run("sprint", "add", sid, "ghost")
    assert rc != 0
    assert "ghost" in capsys.readouterr().err.lower()


def test_add_rejects_non_finite_estimate(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    capsys.readouterr()
    rc = _run("sprint", "add", sid, "w1", "--estimate", "nan")
    assert rc != 0
    assert "finite" in capsys.readouterr().err.lower()


def test_retro_note_before_close_is_rejected(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    capsys.readouterr()
    rc = _run("sprint", "retro", sid, "--note", "too early")  # still `planning`
    assert rc != 0
    assert "closed" in capsys.readouterr().err.lower()


def test_whitespace_only_retro_note_is_treated_as_no_note(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    _run("sprint", "close", sid)
    capsys.readouterr()
    assert _run("sprint", "retro", sid, "--note", "   ") == 0  # falls through to view mode
    capsys.readouterr()
    assert _run("reminders", "--json") == 0
    reminders = json.loads(capsys.readouterr().out)
    # a whitespace-only "note" must NOT have permanently silenced the retro nudge
    assert any(r["kind"] == "sprint-needs-retro" and r["item_id"] == sid for r in reminders)


def test_add_rejects_unknown_sprint(pm_home: Path, capsys) -> None:
    _item("w1", capsys)
    rc = _run("sprint", "add", "sprint:ghostghost", "w1")
    assert rc != 0


def test_start_requires_at_least_one_item(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    capsys.readouterr()
    rc = _run("sprint", "start", sid)
    assert rc != 0
    assert "no items" in capsys.readouterr().err.lower()


def test_start_freezes_scope(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _item("w2", capsys)
    _run("sprint", "add", sid, "w1")
    capsys.readouterr()
    assert _run("sprint", "start", sid) == 0
    rc = _run("sprint", "add", sid, "w2")
    assert rc != 0  # scope frozen — w2 must be rejected, not silently added


def test_close_then_reclose_is_an_error(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    capsys.readouterr()
    assert _run("sprint", "close", sid) == 0
    rc = _run("sprint", "close", sid)
    assert rc != 0


def test_close_from_planning_is_rejected(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    capsys.readouterr()
    rc = _run("sprint", "close", sid)  # never started
    assert rc != 0
    assert "active" in capsys.readouterr().err.lower()


def test_reminders_surfaces_stale_planning_end_to_end(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    capsys.readouterr()
    # Force the sprint's `updated_at` far enough into the past by writing the event directly
    # (the CLI has no "backdate" flag, and this exercises the real fold + command path, not a
    # hand-built Sprint object like the unit tests do).
    from pmlib.commands._shared import open_store
    from pmlib.events import Event, EventKind, new_event

    fresh = new_event(EventKind.SPRINT_CREATED, "backdated", {"name": "Old"})
    old = Event(
        id=fresh.id,
        ts="2000-01-01T00:00:00.000000Z",
        kind=fresh.kind,
        item_id=fresh.item_id,
        payload=fresh.payload,
    )
    open_store().append(old)
    assert _run("reminders", "--json") == 0
    reminders = json.loads(capsys.readouterr().out)
    kinds = {(r["kind"], r["item_id"]) for r in reminders}
    assert ("sprint-stale-planning", "backdated") in kinds
    assert ("sprint-stale-planning", sid) not in kinds  # freshly created — not stale yet


def test_reminders_surfaces_ready_to_close_end_to_end(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    for target in (
        "classified",
        "ticketed",
        "ready",
        "scoped",
        "queued",
        "dispatched",
        "implementing",
        "reviewing",
        "verifying",
        "pr-open",
        "ci-pending",
        "ship-ready",
    ):
        _run("record", "transition", "w1", target)
    _run("record", "evidence", "w1", "deployment", "https://deploy/w1")
    _run("record", "transition", "w1", "deployed")
    _run("record", "transition", "w1", "done")
    capsys.readouterr()
    assert _run("reminders", "--json") == 0
    reminders = json.loads(capsys.readouterr().out)
    assert any(r["kind"] == "sprint-ready-to-close" and r["item_id"] == sid for r in reminders)


def test_retro_note_then_view(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    _run("sprint", "close", sid)
    capsys.readouterr()
    assert _run("sprint", "retro", sid, "--note", "went well") == 0
    capsys.readouterr()
    assert _run("sprint", "retro", sid, "--json") == 0
    body = json.loads(capsys.readouterr().out)
    assert body["sprint"]["retro_notes"] == ["went well"]


def test_status_reflects_live_item_state_not_a_stale_copy(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    capsys.readouterr()
    _run("sprint", "status", sid, "--json")
    before = json.loads(capsys.readouterr().out)
    assert before["rows"][0]["done"] is False
    # advance the underlying work item all the way to done, without touching the sprint at all
    for target in (
        "classified",
        "ticketed",
        "ready",
        "scoped",
        "queued",
        "dispatched",
        "implementing",
        "reviewing",
        "verifying",
        "pr-open",
        "ci-pending",
        "ship-ready",
    ):
        _run("record", "transition", "w1", target)
    _run("record", "evidence", "w1", "deployment", "https://deploy/w1")
    _run("record", "transition", "w1", "deployed")
    _run("record", "transition", "w1", "done")
    capsys.readouterr()
    _run("sprint", "status", sid, "--json")
    after = json.loads(capsys.readouterr().out)
    assert after["rows"][0]["done"] is True  # the join picked up the live state, no drift


def test_reminders_surfaces_needs_retro(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    _run("sprint", "close", sid)
    capsys.readouterr()
    assert _run("reminders", "--json") == 0
    reminders = json.loads(capsys.readouterr().out)
    assert any(r["kind"] == "sprint-needs-retro" and r["item_id"] == sid for r in reminders)
