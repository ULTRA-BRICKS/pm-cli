"""`pm ingest --source task` end-to-end: from a records file, dedupe, dry-run, unknown source."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pmlib.cli import main


def _run(*argv: str) -> int:
    return main(list(argv))


def _records_file(tmp_path: Path) -> str:
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps(
            [
                {"id": "#12", "title": "Fix login", "state": "in-progress", "url": "https://gh/12"},
                {"id": "#13", "title": "Docs", "state": "todo", "url": "https://gh/13"},
            ]
        ),
        encoding="utf-8",
    )
    return str(path)


def test_ingest_from_file_populates_queue(pm_home: Path, tmp_path: Path, capsys) -> None:
    assert _run("ingest", "--source", "task", "--from-file", _records_file(tmp_path)) == 0
    capsys.readouterr()
    _run("queue", "--json")
    ids = {it["id"] for it in json.loads(capsys.readouterr().out)}
    assert ids == {"task:#12", "task:#13"}


def test_ingest_is_idempotent(pm_home: Path, tmp_path: Path, capsys) -> None:
    rf = _records_file(tmp_path)
    _run("ingest", "--source", "task", "--from-file", rf)
    capsys.readouterr()
    _run("ingest", "--source", "task", "--from-file", rf, "--json")
    summary = json.loads(capsys.readouterr().out)
    assert summary["new_items"] == 0  # nothing new the second time


def test_ingest_dry_run_writes_nothing(pm_home: Path, tmp_path: Path, capsys) -> None:
    _run("ingest", "--source", "task", "--from-file", _records_file(tmp_path), "--dry-run")
    capsys.readouterr()
    _run("queue", "--json")
    assert json.loads(capsys.readouterr().out) == []


def test_ingest_rejects_unknown_source(pm_home: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:  # argparse rejects an invalid --source choice
        _run("ingest", "--source", "nope")
    assert exc.value.code != 0
    assert "invalid choice" in capsys.readouterr().err.lower()


def test_ingest_nonexistent_repo_dir_reports_that(pm_home: Path, tmp_path: Path, capsys) -> None:
    missing = str(tmp_path / "does-not-exist")
    rc = _run("ingest", "--source", "task", "--repo-dir", missing)
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "repo-dir" in err and "task" not in err.split("repo-dir")[0]  # not "task not found"
