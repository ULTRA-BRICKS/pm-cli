"""`pm executors` and `pm route` end-to-end (read-only, writes no events)."""

from __future__ import annotations

import json
from pathlib import Path

from pmlib.cli import main


def _run(*argv: str) -> int:
    return main(list(argv))


def _execs_file(tmp_path: Path) -> str:
    path = tmp_path / "execs.json"
    path.write_text(json.dumps([
        {"id": "junior", "harness": "gemini", "model": "gemini-2.5-flash",
         "reliability": "healthy", "limit_remaining": 0.9},
        {"id": "senior", "harness": "claude-code", "model": "claude-opus-4-8",
         "reliability": "healthy", "limit_remaining": 0.7}]), encoding="utf-8")
    return str(path)


def test_pm_executors_lists_the_ledger(pm_home: Path, tmp_path: Path, capsys) -> None:
    assert _run("executors", "--from-file", _execs_file(tmp_path), "--json") == 0
    ids = {e["id"] for e in json.loads(capsys.readouterr().out)}
    assert ids == {"junior", "senior"}


def test_pm_route_recommends_for_an_item(pm_home: Path, tmp_path: Path, capsys) -> None:
    _run("record", "intake", "--id", "w", "--title", "task", "--privacy", "remote-allowed")
    capsys.readouterr()
    assert _run("route", "w", "--risk", "high", "--from-file", _execs_file(tmp_path), "--json") == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["item_id"] == "w" and rec["action"] == "route" and rec["executor_id"] == "senior"


def test_pm_route_writes_no_events(pm_home: Path, tmp_path: Path, capsys) -> None:
    _run("record", "intake", "--id", "w", "--title", "t", "--privacy", "remote-allowed")
    capsys.readouterr()
    _run("queue", "--json")
    before = capsys.readouterr().out
    _run("route", "w", "--from-file", _execs_file(tmp_path))
    capsys.readouterr()
    _run("queue", "--json")
    assert capsys.readouterr().out == before  # nothing changed — route is read-only


def test_pm_route_all_items(pm_home: Path, tmp_path: Path, capsys) -> None:
    _run("record", "intake", "--id", "a", "--title", "A", "--privacy", "remote-allowed")
    _run("record", "intake", "--id", "b", "--title", "B", "--privacy", "remote-allowed")
    capsys.readouterr()
    assert _run("route", "--from-file", _execs_file(tmp_path), "--json") == 0
    recs = json.loads(capsys.readouterr().out)
    assert {r["item_id"] for r in recs} == {"a", "b"}


def test_pm_route_unknown_item_errors(pm_home: Path, tmp_path: Path, capsys) -> None:
    rc = _run("route", "ghost", "--from-file", _execs_file(tmp_path))
    assert rc != 0
    assert "ghost" in capsys.readouterr().err.lower()
