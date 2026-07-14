"""`pm link`, `pm timer`, and `pm reminders` end-to-end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pmlib.cli import main


def _run(*argv: str) -> int:
    return main(list(argv))


def _two_items() -> None:
    _run("record", "intake", "--id", "a", "--title", "A")
    _run("record", "intake", "--id", "b", "--title", "B")


def test_link_records_an_edge(pm_home: Path, capsys) -> None:
    _two_items()
    capsys.readouterr()
    assert _run("link", "a", "blocks", "b", "--reason", "schema first") == 0
    _run("why", "a")
    assert "blocks" in capsys.readouterr().out


def test_link_rejects_unknown_type(pm_home: Path, capsys) -> None:
    _two_items()
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:  # argparse rejects an invalid edge type
        _run("link", "a", "frobnicates", "b")
    assert exc.value.code != 0
    assert "invalid choice" in capsys.readouterr().err.lower()


def test_link_rejects_unknown_item(pm_home: Path, capsys) -> None:
    _two_items()
    capsys.readouterr()
    rc = _run("link", "a", "blocks", "ghost")
    assert rc != 0
    assert "ghost" in capsys.readouterr().err.lower()


def test_timer_records_and_reminders_fire(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "a", "--title", "A")
    capsys.readouterr()
    assert _run("timer", "a", "--at", "2000-01-01T00:00:00Z", "--reason", "overdue") == 0
    capsys.readouterr()
    assert _run("reminders", "--json") == 0
    reminders = json.loads(capsys.readouterr().out)
    assert any(r["kind"] == "timer-expired" and r["item_id"] == "a" for r in reminders)


def test_reminders_empty_when_nothing_due(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "a", "--title", "A")
    capsys.readouterr()
    assert _run("reminders", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_blocker_cleared_reminder_via_cli(pm_home: Path, capsys) -> None:
    _two_items()
    _run("link", "a", "blocks", "b", "--reason", "x")
    # push a through to a terminal state so its block on b clears
    for target in ("classified", "ticketed", "ready", "scoped", "queued", "dispatched",
                   "implementing", "reviewing", "verifying", "pr-open", "ci-pending",
                   "ship-ready"):
        _run("record", "transition", "a", target)
    _run("record", "evidence", "a", "deployment", "https://deploy/a")
    _run("record", "transition", "a", "deployed")
    _run("record", "transition", "a", "done")
    capsys.readouterr()
    _run("reminders", "--json")
    reminders = json.loads(capsys.readouterr().out)
    assert any(r["kind"] == "blocker-cleared" and r["item_id"] == "b" for r in reminders)
