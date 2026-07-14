"""`pm reconcile --source pr|rig` end-to-end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pmlib.cli import main

_URL = "https://github.com/x/y/pull/1"


def _run(*argv: str) -> int:
    return main(list(argv))


def _facts_file(tmp_path: Path, ci: str = "success", pr_state: str = "open") -> str:
    path = tmp_path / "facts.json"
    path.write_text(json.dumps([
        {"item_id": "w", "pr_url": _URL, "pr_state": pr_state, "ci": ci}]), encoding="utf-8")
    return str(path)


def _seed_pr_item() -> None:
    _run("record", "intake", "--id", "w", "--title", "feature")
    for s in ("classified", "ticketed", "ready", "scoped", "queued", "dispatched",
              "implementing", "reviewing", "verifying", "pr-open"):
        _run("record", "transition", "w", s)
    _run("record", "evidence", "w", "pr", _URL)


def test_pr_reconcile_advances_on_green_ci(pm_home: Path, tmp_path: Path, capsys) -> None:
    _seed_pr_item()
    capsys.readouterr()
    assert _run("reconcile", "--source", "pr", "--from-file", _facts_file(tmp_path)) == 0
    capsys.readouterr()
    _run("queue", "--json")
    row = next(r for r in json.loads(capsys.readouterr().out) if r["id"] == "w")
    assert row["state"] == "ship-ready"


def test_pr_reconcile_is_idempotent(pm_home: Path, tmp_path: Path, capsys) -> None:
    _seed_pr_item()
    ff = _facts_file(tmp_path)
    _run("reconcile", "--source", "pr", "--from-file", ff)
    capsys.readouterr()
    _run("reconcile", "--source", "pr", "--from-file", ff, "--json")
    summary = json.loads(capsys.readouterr().out)
    assert summary["transitions"] == 0  # converged: a re-run changes nothing


def test_merged_pr_does_not_advance_past_ship_ready(pm_home: Path, tmp_path: Path, capsys) -> None:
    _seed_pr_item()
    _run("reconcile", "--source", "pr", "--from-file", _facts_file(tmp_path))  # -> ship-ready
    capsys.readouterr()
    _run("reconcile", "--source", "pr", "--from-file",
         _facts_file(tmp_path, pr_state="merged"))
    capsys.readouterr()
    _run("queue", "--json")
    row = next(r for r in json.loads(capsys.readouterr().out) if r["id"] == "w")
    assert row["state"] == "ship-ready"  # merge recorded, but the fork boundary holds


def test_rig_reconcile_reports_config_drift(pm_home: Path, tmp_path: Path, capsys) -> None:
    health = tmp_path / "rig.json"
    health.write_text(json.dumps(
        {"returncode": 4, "stdout": "", "stderr": "unknown key harness.kinds; rejected"}),
        encoding="utf-8")
    assert _run("reconcile", "--source", "rig", "--from-file", str(health), "--json") == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "config-drift"


def test_reconcile_rejects_unknown_source(pm_home: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _run("reconcile", "--source", "nope")
    assert exc.value.code != 0
