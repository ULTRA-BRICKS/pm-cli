"""`pm why --json` (read-only; mirrors `pm route --json`'s pattern) end-to-end.

Feeds tg-cli's `/next <ticket-id>` Telegram command (tg-cli#next-command) — the JSON shape here
is a stable contract another repo's process consumes, so it gets its own test file the way
`pm route --json` did in test_route_cli.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from pmlib.cli import main


def _run(*argv: str) -> int:
    return main(list(argv))


def test_why_json_emits_identity_and_state(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "Fix login")
    capsys.readouterr()
    assert _run("why", "w1", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == "w1"
    assert data["title"] == "Fix login"
    assert data["state"] == "seen"


def test_why_json_lists_pm_labels_separately_from_source_labels(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "x")
    capsys.readouterr()
    assert _run("why", "w1", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert any(label.startswith("pm.stage:") for label in data["pm_labels"])
    assert any(label.startswith("pm.health:") for label in data["pm_labels"])
    assert data["labels"] == []  # raw source labels (task-cli etc.), untouched by pm_labels


def test_why_json_next_moves_list_legal_transitions(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "x")
    capsys.readouterr()
    assert _run("why", "w1", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["next"]["terminal"] is False
    assert data["next"]["unknown_state"] is False
    move_states = {m["state"] for m in data["next"]["moves"]}
    assert "classified" in move_states


def test_why_json_next_moves_report_missing_evidence(pm_home: Path, capsys) -> None:
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
        assert _run("record", "transition", "w1", target) == 0
    capsys.readouterr()
    assert _run("why", "w1", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    deployed = next(m for m in data["next"]["moves"] if m["state"] == "deployed")
    assert deployed["missing_evidence"] == ["deployment"]


def test_why_json_terminal_state_has_no_moves(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "x")
    capsys.readouterr()
    assert _run("record", "transition", "w1", "cancelled", "--force") == 0
    capsys.readouterr()
    assert _run("why", "w1", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["next"] == {"terminal": True, "unknown_state": False, "moves": []}


def test_why_json_surfaces_evidence_and_history(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "x")
    _run("record", "transition", "w1", "classified")
    _run("record", "evidence", "w1", "pr", "https://gh/pr/1")
    capsys.readouterr()
    assert _run("why", "w1", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["evidence"][0]["kind"] == "pr"
    assert data["evidence"][0]["uri"] == "https://gh/pr/1"
    assert data["history"] == [{"from": "seen", "to": "classified", "ts": data["history"][0]["ts"]}]


def test_why_json_unknown_state_has_no_moves(pm_home: Path, capsys) -> None:
    # --force bypasses the FSM entirely, so a target state absent from the machine (a typo, or a
    # state retired in a machine-config change) can land in the store. `pm why --json` must say
    # so explicitly (unknown_state: true) rather than silently reporting terminal/no-moves — a
    # consumer like tg-cli's /next needs to tell "nothing left to do" apart from "this state is
    # not recognized, something is wrong".
    _run("record", "intake", "--id", "w1", "--title", "x")
    capsys.readouterr()
    assert _run("record", "transition", "w1", "typo", "--force") == 0
    capsys.readouterr()
    assert _run("why", "w1", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "typo"
    assert data["next"] == {"terminal": False, "unknown_state": True, "moves": []}


def test_why_json_unknown_item_errors(pm_home: Path, capsys) -> None:
    rc = _run("why", "ghost", "--json")
    assert rc != 0
    assert "no work item" in capsys.readouterr().err.lower()


def test_why_json_writes_no_events(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "t")
    capsys.readouterr()
    _run("queue", "--json")
    before = capsys.readouterr().out
    _run("why", "w1", "--json")
    capsys.readouterr()
    _run("queue", "--json")
    assert capsys.readouterr().out == before  # nothing changed — why is read-only


def test_why_text_output_unchanged_by_json_flag(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "w1", "--title", "Fix login")
    capsys.readouterr()
    assert _run("why", "w1") == 0
    out = capsys.readouterr().out
    assert "w1" in out
    assert "[seen]" in out
    assert "{" not in out  # plain text, not JSON, when --json is omitted
