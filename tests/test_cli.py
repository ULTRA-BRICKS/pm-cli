"""End-to-end CLI: record intake -> transition -> evidence -> queue -> why, plus doctor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pmlib.cli import main


def _run(*argv: str) -> int:
    return main(list(argv))


def test_help_runs_without_heavy_deps(capsys) -> None:
    with pytest.raises(SystemExit) as exc:  # argparse --help exits 0
        _run("--help")
    assert exc.value.code == 0
    assert "pm" in capsys.readouterr().out


def test_version(capsys) -> None:
    assert _run("version") == 0
    out = capsys.readouterr().out
    assert out.strip()  # prints a version string


def test_record_intake_and_queue(pm_home: Path, capsys) -> None:
    assert _run("record", "intake", "--id", "w1", "--title", "Fix login") == 0
    capsys.readouterr()
    assert _run("queue", "--json") == 0
    out = capsys.readouterr().out
    items = json.loads(out)
    assert items[0]["id"] == "w1"
    assert items[0]["state"] == "seen"


def test_record_transition_flow(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "x")
    assert _run("record", "transition", "w1", "classified") == 0
    capsys.readouterr()
    _run("queue", "--json")
    items = json.loads(capsys.readouterr().out)
    assert items[0]["state"] == "classified"


def test_illegal_transition_is_rejected_at_write_time(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "x")
    capsys.readouterr()
    rc = _run("record", "transition", "w1", "deployed")  # illegal from seen
    assert rc != 0
    err = capsys.readouterr().err
    assert "illegal" in err.lower()


def test_evidence_gate_enforced_at_write_time(pm_home: Path, capsys) -> None:
    # Walk to ship-ready then attempt deployed without deployment evidence.
    _run("record", "intake", "--id", "w1", "--title", "x")
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
        assert _run("record", "transition", "w1", target) == 0, f"failed to reach {target}"
    capsys.readouterr()
    rc = _run("record", "transition", "w1", "deployed")  # needs deployment evidence
    assert rc != 0
    assert "evidence" in capsys.readouterr().err.lower()


def test_why_explains_current_state(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "Fix login")
    capsys.readouterr()
    assert _run("why", "w1") == 0
    out = capsys.readouterr().out
    assert "w1" in out
    assert "seen" in out
    assert "classified" in out  # lists the legal next states


def test_doctor_passes_on_clean_store(pm_home: Path) -> None:
    assert _run("doctor") == 0


def test_force_bypasses_gates_at_write_time(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "x")
    capsys.readouterr()
    assert _run("record", "transition", "w1", "deployed", "--force") == 0
    capsys.readouterr()
    _run("queue", "--json")
    items = json.loads(capsys.readouterr().out)
    assert items[0]["state"] == "deployed"  # the illegal jump went through under --force


def test_record_note_and_evidence_cli(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "x")
    assert _run("record", "note", "w1", "a stray thought") == 0
    assert _run("record", "evidence", "w1", "pr", "https://gh/pr/1") == 0
    capsys.readouterr()
    _run("why", "w1")
    assert "pr" in capsys.readouterr().out  # evidence surfaced


def test_evidence_on_unknown_item_errors(pm_home: Path, capsys) -> None:
    rc = _run("record", "evidence", "ghost", "pr", "u")
    assert rc != 0
    assert "no work item" in capsys.readouterr().err.lower()


def test_intake_custom_seed_state(pm_home: Path, capsys) -> None:
    assert _run("record", "intake", "--id", "w1", "--title", "x", "--state", "ready") == 0
    capsys.readouterr()
    _run("queue", "--json")
    assert json.loads(capsys.readouterr().out)[0]["state"] == "ready"


def test_intake_rejects_unknown_seed_state(pm_home: Path, capsys) -> None:
    rc = _run("record", "intake", "--id", "w1", "--title", "x", "--state", "garbage")
    assert rc != 0
    assert "unknown initial state" in capsys.readouterr().err.lower()


def test_queue_filters_by_category_and_state(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "a")  # seen (intake)
    _run("record", "intake", "--id", "w2", "--title", "b", "--state", "ready")  # triage
    capsys.readouterr()
    _run("queue", "--json", "--category", "triage")
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["w2"]
    _run("queue", "--json", "--state", "seen")
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["w1"]


def test_why_unknown_item_errors(pm_home: Path, capsys) -> None:
    rc = _run("why", "ghost")
    assert rc != 0
    assert "no work item" in capsys.readouterr().err.lower()


def test_doctor_fails_on_unwritable_store(tmp_path, monkeypatch, capsys) -> None:
    blocked = tmp_path / "ro" / "pm"
    blocked.parent.mkdir()
    blocked.parent.chmod(0o500)  # parent not writable -> mkdir of child fails
    monkeypatch.setenv("PM_HOME", str(blocked))
    try:
        rc = _run("doctor")
        assert rc != 0
        assert "FAIL" in capsys.readouterr().out
    finally:
        blocked.parent.chmod(0o700)
