"""`pm ingest --source telegram` + `pm dlq` end-to-end: capture, process, list the ledger."""

from __future__ import annotations

import json
from pathlib import Path

from pmlib.cli import main


def _run(*argv: str) -> int:
    return main(list(argv))


def _records_file(tmp_path: Path) -> str:
    path = tmp_path / "tg.json"
    path.write_text(json.dumps([
        {"ts": 100, "id": 8328, "direction": "user", "from": "Alex", "text": "deploy", "pane": "%4"},
        {"ts": 200, "id": 8331, "direction": "user", "from": "Alex", "text": "status?", "pane": None},
    ]), encoding="utf-8")
    return str(path)


def test_telegram_ingest_captures_and_processes(pm_home: Path, tmp_path: Path, capsys) -> None:
    rc = _run("ingest", "--source", "telegram", "--from-file", _records_file(tmp_path))
    assert rc == 0
    capsys.readouterr()
    # each captured message became a WorkItem
    _run("queue", "--json")
    ids = {it["id"] for it in json.loads(capsys.readouterr().out)}
    assert ids == {"intake:tg:8328", "intake:tg:8331"}


def test_dlq_lists_the_ledger(pm_home: Path, tmp_path: Path, capsys) -> None:
    _run("ingest", "--source", "telegram", "--from-file", _records_file(tmp_path))
    capsys.readouterr()
    assert _run("dlq", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["id"] for r in rows} == {"tg:8328", "tg:8331"}
    assert all(r["status"] == "processed" for r in rows)


def test_dlq_status_filter(pm_home: Path, tmp_path: Path, capsys) -> None:
    _run("ingest", "--source", "telegram", "--from-file", _records_file(tmp_path))
    capsys.readouterr()
    _run("dlq", "--json", "--status", "deadletter")
    assert json.loads(capsys.readouterr().out) == []  # nothing deadlettered on the happy path


def test_telegram_ingest_is_idempotent(pm_home: Path, tmp_path: Path, capsys) -> None:
    rf = _records_file(tmp_path)
    _run("ingest", "--source", "telegram", "--from-file", rf)
    capsys.readouterr()
    _run("ingest", "--source", "telegram", "--from-file", rf, "--json")
    summary = json.loads(capsys.readouterr().out)
    assert summary["captured"] == 0  # nothing new the second time


def test_dlq_empty_store(pm_home: Path, capsys) -> None:
    assert _run("dlq") == 0
    assert "empty" in capsys.readouterr().out.lower()


def test_dlq_surfaces_a_deadlettered_intake(pm_home: Path, capsys) -> None:
    """A dead-lettered request is durably recorded and visible — never silently lost."""
    from pmlib.events import EventKind, new_event
    from pmlib.store import EventStore, resolve_store_dir

    store = EventStore(resolve_store_dir())
    store.append(new_event(EventKind.INTAKE_CAPTURED, "", {
        "id": "tg:9", "source": "telegram", "external_id": "9",
        "sender": "Alex", "text": "boom", "ts_ms": 1, "pane": ""}))
    store.append(new_event(EventKind.INTAKE_DEADLETTER, "tg:9", {"reason": "classifier exploded"}))
    assert _run("dlq", "--json", "--status", "deadletter") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["tg:9"]
    assert rows[0]["last_error"] == "classifier exploded"
